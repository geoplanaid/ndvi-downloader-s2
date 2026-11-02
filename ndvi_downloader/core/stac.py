"""Search Sentinel-2 L2A scenes on the Earth Search STAC API (Element 84 / AWS).

No account or API key is needed: the catalogue and the Cloud-Optimized GeoTIFFs
are public. Only the standard library is used so the module also runs outside QGIS.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime

STAC_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-l2a"
USER_AGENT = "QGIS-NDVI-Downloader-S2 (Geoplana)"


@dataclass
class SceneItem:
    id: str
    datetime: datetime
    cloud_cover: float
    epsg: int
    tile: str
    red: str
    nir: str
    scl: str
    visual: str = ""
    scale: float = 0.0001
    offset: float = 0.0
    sequence: int = 0
    footprint: dict = None  # GeoJSON geometry of the valid data area (EPSG:4326)
    properties: dict = field(default_factory=dict, repr=False)

    @property
    def date(self) -> date:
        return self.datetime.date()


class StacError(RuntimeError):
    pass


def _post_json(url, body, timeout=60, retries=3):
    data = json.dumps(body).encode("utf-8")
    last_error = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/geo+json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last_error = StacError(f"STAC API HTTP {exc.code}: {detail}")
            if exc.code < 500 and exc.code != 429:
                raise last_error
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = StacError(f"Gagal terhubung ke STAC API: {exc}")
        time.sleep(2 * (attempt + 1))
    raise last_error


def _parse_datetime(value):
    value = value.replace("Z", "+00:00")
    # Python < 3.11 cannot parse more than 6 fractional digits
    if "." in value:
        head, tail = value.split(".", 1)
        frac, _, tz = tail.partition("+")
        value = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value)


def _band_scaling(asset, props):
    """Return (scale, offset) turning DN into surface reflectance.

    ESA processing baseline >= 04.00 stores DN = 10000 * reflectance + 1000.
    Earth Search removes that +1000 in its COGs ("earthsearch:boa_offset_applied")
    yet still advertises offset -0.1 in raster:bands, so the advertised offset
    must not be trusted: applying it twice pushes red below zero and NDVI to 1.
    """
    bands = asset.get("raster:bands") or [{}]
    scale = float(bands[0].get("scale", 0.0001))
    if props.get("earthsearch:boa_offset_applied", False):
        return scale, 0.0
    try:
        baseline = float(props.get("s2:processing_baseline", "0"))
    except ValueError:
        baseline = 0.0
    return scale, (-0.1 if baseline >= 4.0 else 0.0)


def _is_public_cog(href):
    # Some reprocessed items only point at JP2 files in the requester-pays
    # s3://sentinel-s2-l2a bucket, which cannot be read anonymously.
    return href.startswith("https://") and href.lower().endswith((".tif", ".tiff"))


def parse_feature(feature):
    """Turn a STAC feature into a SceneItem, or None when readable COG assets are missing."""
    props = feature.get("properties", {})
    assets = feature.get("assets", {})
    if not all(k in assets and _is_public_cog(assets[k].get("href", "")) for k in ("red", "nir", "scl")):
        return None
    epsg = props.get("proj:epsg")
    if epsg is None and str(props.get("proj:code", "")).upper().startswith("EPSG:"):
        epsg = int(props["proj:code"].split(":")[1])
    if epsg is None:
        return None

    item_id = feature["id"]
    # Earth Search ids look like S2A_49MDM_20190628_1_L2A; the sequence number
    # grows when ESA reprocesses a scene.
    parts = item_id.split("_")
    tile = props.get("s2:mgrs_tile") or (props.get("grid:code") or "").replace("MGRS-", "")
    if not tile and len(parts) > 1:
        tile = parts[1]
    try:
        sequence = int(parts[3])
    except (IndexError, ValueError):
        sequence = 0

    scale, offset = _band_scaling(assets["red"], props)
    return SceneItem(
        id=item_id,
        datetime=_parse_datetime(props["datetime"]),
        cloud_cover=float(props.get("eo:cloud_cover", 0.0)),
        epsg=int(epsg),
        tile=tile,
        red=assets["red"]["href"],
        nir=assets["nir"]["href"],
        scl=assets["scl"]["href"],
        visual=assets.get("visual", {}).get("href", ""),
        scale=scale,
        offset=offset,
        sequence=sequence,
        footprint=feature.get("geometry"),
        properties=props,
    )


def deduplicate(items):
    """Keep one item per (tile, acquisition date), preferring the latest reprocessing."""
    best = {}
    for item in items:
        key = (item.tile, item.date)
        current = best.get(key)
        if current is None or item.sequence > current.sequence:
            best[key] = item
    return sorted(best.values(), key=lambda i: (i.datetime, i.tile))


def _year_windows(start, end):
    """Split [start, end] into calendar-year windows.

    One request spanning many years makes Earth Search time out (HTTP 502),
    while yearly requests stay fast and paginate cleanly.
    """
    cursor = start
    while cursor <= end:
        stop = min(end, date(cursor.year, 12, 31))
        yield cursor, stop
        cursor = date(cursor.year + 1, 1, 1)


def _search_window(body, url, is_canceled, max_items):
    items = []
    request_url = url
    while True:
        if is_canceled and is_canceled():
            return items
        page = _post_json(request_url, body)
        for feature in page.get("features", []):
            item = parse_feature(feature)
            if item is not None:
                items.append(item)
        next_link = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not next_link or not page.get("features") or len(items) >= max_items:
            return items
        request_url = next_link.get("href", url)
        if next_link.get("body"):
            body = next_link["body"] if not next_link.get("merge") else {**body, **next_link["body"]}


def search_scenes(
    geometry,
    start,
    end,
    max_cloud=60,
    max_items=3000,
    page_size=200,
    is_canceled=None,
    on_page=None,
    url=STAC_SEARCH_URL,
):
    """Return deduplicated SceneItems intersecting a GeoJSON geometry (EPSG:4326), sorted by date.

    ``start`` and ``end`` are ``datetime.date`` objects (inclusive).
    ``on_page(n_found, year)`` is called after each yearly window.
    """
    items = []
    for win_start, win_end in _year_windows(start, end):
        if (is_canceled and is_canceled()) or len(items) >= max_items:
            break
        body = {
            "collections": [COLLECTION],
            "intersects": geometry,
            "datetime": f"{win_start.isoformat()}T00:00:00Z/{win_end.isoformat()}T23:59:59Z",
            "limit": page_size,
            "query": {"eo:cloud_cover": {"lte": max_cloud}},
        }
        items.extend(_search_window(body, url, is_canceled, max_items - len(items)))
        if on_page:
            on_page(len(items), win_start.year)
    if is_canceled and is_canceled():
        return []
    return deduplicate(items[:max_items])
