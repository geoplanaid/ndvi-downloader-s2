"""Monthly NDVI composites per area: search, read, composite, write, report."""

import csv
import json
import os
import re
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from osgeo import ogr

from .raster import (
    DEFAULT_VALID_SCL,
    build_area_grid,
    configure_gdal_thread,
    polygon_mask,
    scene_ndvi_on_grid,
    write_geotiff,
)
from .stac import search_scenes

METHODS = ("median", "max", "mean")


@dataclass
class Area:
    name: str
    geojson: dict  # EPSG:4326 geometry


@dataclass
class MonthResult:
    area: str
    month: str  # YYYY-MM
    status: str  # ok | exists | no_scenes | low_coverage | too_large | error | canceled
    path: str = ""
    n_scenes: int = 0
    coverage: float = 0.0
    ndvi_mean: float = float("nan")
    area_ha: float = 0.0
    message: str = ""
    retried: bool = False  # filled with scenes above the cloud limit


@dataclass
class Settings:
    out_dir: str
    start_month: date
    end_month: date
    resolution: float = 10.0
    method: str = "median"
    max_cloud: float = 80.0
    min_coverage: float = 0.5
    max_area_ha: float = 10_000.0
    valid_scl: tuple = DEFAULT_VALID_SCL
    skip_existing: bool = True
    retry_cloudy: bool = True  # empty/cloudy month: also use scenes above max_cloud, masked per pixel
    workers: int = 8
    extra: dict = field(default_factory=dict)


def month_range(start, end):
    """Every first-of-month from start to end inclusive."""
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def month_end(d):
    nxt = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def fmt_ha(ha):
    """Indonesian number format: 12.345 ha / 44,1 ha."""
    text = f"{ha:,.1f}" if ha < 100 else f"{ha:,.0f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".") + " ha"


def safe_name(name):
    name = re.sub(r"[^\w\-]+", "_", str(name), flags=re.UNICODE).strip("_")
    return name[:60] or "area"


def output_path(out_dir, area_name, month):
    folder = os.path.join(out_dir, safe_name(area_name))
    return os.path.join(folder, f"NDVI_{safe_name(area_name)}_{month:%Y-%m}.tif")


def composite(stack, method):
    """Per-pixel composite of a (n, h, w) NaN-masked stack; returns (ndvi, count)."""
    count = np.sum(~np.isnan(stack), axis=0).astype(np.uint16)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN pixels
        if method == "max":
            out = np.nanmax(stack, axis=0)
        elif method == "mean":
            out = np.nanmean(stack, axis=0)
        else:
            out = np.nanmedian(stack, axis=0)
    out = out.astype(np.float32)
    out[count == 0] = np.nan
    return out, count


def drop_redundant_tiles(items, aoi_geojson):
    """Per date, keep a single tile when its footprint already covers the whole area."""
    aoi = ogr.CreateGeometryFromJson(json.dumps(aoi_geojson))
    by_date = defaultdict(list)
    for item in items:
        by_date[item.date].append(item)
    kept = []
    for day in sorted(by_date):
        group = by_date[day]
        covering = []
        if len(group) > 1 and aoi is not None:
            for item in group:
                fp = ogr.CreateGeometryFromJson(json.dumps(item.footprint)) if item.footprint else None
                if fp is not None and fp.Contains(aoi):
                    covering.append(item)
        kept.extend([min(covering, key=lambda i: i.cloud_cover)] if covering else group)
    return kept


def process_area(area, settings, log=None, progress=None, is_canceled=None):
    """Create one GeoTIFF per month for ``area``. Returns a list of MonthResult.

    ``progress(fraction)`` receives 0..1 for this area.
    """
    log = log or (lambda msg: None)
    progress = progress or (lambda f: None)
    is_canceled = is_canceled or (lambda: False)
    months = month_range(settings.start_month, settings.end_month)

    area_grid = build_area_grid(area.geojson, settings.resolution)
    ha = area_grid.area_ha
    if ha > settings.max_area_ha:
        msg = f"luas {fmt_ha(ha)} melebihi batas {fmt_ha(settings.max_area_ha)}"
        log(f"[{area.name}] DILEWATI: {msg}")
        return [MonthResult(area.name, f"{m:%Y-%m}", "too_large", area_ha=ha, message=msg) for m in months]

    results = {}
    todo = []
    for m in months:
        path = output_path(settings.out_dir, area.name, m)
        if settings.skip_existing and os.path.exists(path):
            results[m] = MonthResult(area.name, f"{m:%Y-%m}", "exists", path=path, area_ha=ha,
                                     message="file sudah ada")
        else:
            todo.append(m)
    if not todo:
        log(f"[{area.name}] semua bulan sudah ada, dilewati.")
        return [results[m] for m in months]

    log(f"[{area.name}] {fmt_ha(ha)}, grid {area_grid.grid.width}x{area_grid.grid.height} px "
        f"EPSG:{area_grid.grid.epsg}. Mencari citra...")
    # One search; scenes above the cloud limit are kept aside as a fallback
    search_cloud = 100 if settings.retry_cloudy else settings.max_cloud
    items = search_scenes(area.geojson, todo[0], month_end(todo[-1]), max_cloud=search_cloud,
                          is_canceled=is_canceled)
    items = drop_redundant_tiles(items, area.geojson)
    primary, fallback = defaultdict(list), defaultdict(list)
    for item in items:
        key = date(item.date.year, item.date.month, 1)
        (primary if item.cloud_cover <= settings.max_cloud else fallback)[key].append(item)
    n_primary = sum(len(v) for v in primary.values())
    log(f"[{area.name}] {n_primary} citra untuk {len(todo)} bulan"
        + (f" (+{len(items) - n_primary} citra berawan sebagai cadangan)." if settings.retry_cloudy else "."))
    area_grid.mask = polygon_mask(area_grid)
    n_inside = int(area_grid.mask.sum())

    def read(item):
        configure_gdal_thread()
        if is_canceled():
            return None
        return scene_ndvi_on_grid(item, area_grid, settings.valid_scl)

    total = sum(len(primary[m]) + len(fallback[m]) for m in todo) or 1
    done = 0

    def read_all(pool, month_items):
        nonlocal done
        arrays, errors = [], []
        for fut in [pool.submit(read, item) for item in month_items]:
            try:
                arr = fut.result()
                if arr is not None:
                    arrays.append(arr)
            except Exception as exc:  # one broken scene must not stop the month
                errors.append(str(exc))
            done += 1
            progress(min(1.0, done / total))
        return arrays, errors

    def clear_coverage(arrays):
        if not arrays:
            return None, None, 0.0
        ndvi, count = composite(np.stack(arrays), settings.method)
        cov = float((count[area_grid.mask] > 0).sum()) / n_inside if n_inside else 0.0
        return ndvi, count, cov

    with ThreadPoolExecutor(max_workers=max(1, settings.workers)) as pool:
        for m in todo:
            label = f"{m:%Y-%m}"
            path = output_path(settings.out_dir, area.name, m)
            if is_canceled():
                results[m] = MonthResult(area.name, label, "canceled", area_ha=ha)
                continue
            used = list(primary.get(m, []))
            arrays, errors = read_all(pool, used)
            ndvi, count, coverage = clear_coverage(arrays)

            retried = False
            extra = fallback.get(m, [])
            if coverage < settings.min_coverage and extra and settings.retry_cloudy and not is_canceled():
                before = coverage
                more, more_err = read_all(pool, extra)
                arrays += more
                errors += more_err
                used += extra
                ndvi, count, coverage = clear_coverage(arrays)
                retried = True
                log(f"[{area.name}] {label}: diulang dengan {len(extra)} citra berawan, "
                    f"bebas awan {before:.0%} -> {coverage:.0%}.")
            else:
                done += len(extra)  # fallback not needed for this month
                progress(min(1.0, done / total))
            del arrays
            if is_canceled():
                results[m] = MonthResult(area.name, label, "canceled", area_ha=ha)
                continue

            n_used = len(used) - len(errors)
            if ndvi is None:
                if not used:
                    msg = "tidak ada citra sama sekali" if settings.retry_cloudy else "tidak ada citra di bawah batas awan"
                else:
                    msg = "citra tidak menutupi area" + (f"; {errors[0]}" if errors else "")
                results[m] = MonthResult(area.name, label, "no_scenes", area_ha=ha, message=msg, retried=retried)
                log(f"[{area.name}] {label}: {msg}.")
                continue

            mean = float(np.nanmean(ndvi[area_grid.mask])) if coverage > 0 else float("nan")
            if coverage < settings.min_coverage:
                msg = f"bebas awan {coverage:.0%} < {settings.min_coverage:.0%}"
                if retried:
                    msg += " walau sudah diulang dengan citra berawan"
                results[m] = MonthResult(area.name, label, "low_coverage", n_scenes=n_used, coverage=coverage,
                                         ndvi_mean=mean, area_ha=ha, message=msg, retried=retried)
                log(f"[{area.name}] {label}: dilewati, {msg}.")
                continue

            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_geotiff(
                path, ndvi, area_grid.grid,
                description=f"NDVI {label} ({settings.method})",
                metadata={
                    "AREA": area.name, "MONTH": label, "COMPOSITE": settings.method,
                    "N_SCENES": n_used, "CLEAR_COVERAGE": f"{coverage:.3f}",
                    "CLOUDY_FALLBACK": "YES" if retried else "NO",
                    "SOURCE": "Copernicus Sentinel-2 L2A via Earth Search (Element 84)",
                    "SCENES": ",".join(i.id for i in used),
                },
            )
            notes = []
            if retried:
                notes.append(f"memakai citra di atas batas awan {settings.max_cloud:.0f}%")
            if errors:
                notes.append(f"{len(errors)} citra gagal dibaca")
            results[m] = MonthResult(area.name, label, "ok", path=path, n_scenes=n_used, coverage=coverage,
                                     ndvi_mean=mean, area_ha=ha, message="; ".join(notes), retried=retried)
            log(f"[{area.name}] {label}: OK, {n_used} citra, bebas awan {coverage:.0%}, NDVI rata-rata {mean:.3f}"
                + (" (diulang)" if retried else ""))
    return [results[m] for m in months]


STATUS_LABELS = {
    "ok": "berhasil",
    "exists": "sudah ada",
    "no_scenes": "tidak ada citra",
    "low_coverage": "terlalu berawan",
    "too_large": "area terlalu besar",
    "error": "error",
    "canceled": "dibatalkan",
}


def write_report(path, results):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["area", "bulan", "status", "keterangan", "diulang_citra_berawan", "luas_ha", "jumlah_citra",
                    "bebas_awan_persen", "ndvi_rata2", "file"])
        for r in results:
            w.writerow([
                r.area, r.month, STATUS_LABELS.get(r.status, r.status), r.message, "ya" if r.retried else "tidak",
                f"{r.area_ha:.2f}",
                r.n_scenes, f"{100 * r.coverage:.1f}",
                "" if np.isnan(r.ndvi_mean) else f"{r.ndvi_mean:.4f}", r.path,
            ])
