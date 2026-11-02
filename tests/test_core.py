"""Offline tests. Run with the QGIS Python: python -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from datetime import date

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ndvi_downloader"))

from core.composite import (  # noqa: E402
    Area,
    MonthResult,
    Settings,
    composite,
    fmt_ha,
    month_end,
    month_range,
    output_path,
    process_area,
    safe_name,
    write_report,
)
from core.raster import NODATA, build_area_grid, polygon_mask, utm_epsg, write_geotiff  # noqa: E402
from core.stac import parse_feature  # noqa: E402


def rect(minx, miny, maxx, maxy):
    return {"type": "Polygon", "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]]}


class MonthTests(unittest.TestCase):
    def test_month_range_crosses_year(self):
        months = month_range(date(2023, 11, 1), date(2024, 2, 1))
        self.assertEqual([f"{m:%Y-%m}" for m in months], ["2023-11", "2023-12", "2024-01", "2024-02"])

    def test_month_end(self):
        self.assertEqual(month_end(date(2024, 2, 1)), date(2024, 2, 29))
        self.assertEqual(month_end(date(2023, 12, 1)), date(2023, 12, 31))

    def test_names(self):
        self.assertEqual(safe_name("Blok A/12 (Sawah)"), "Blok_A_12_Sawah")
        self.assertEqual(safe_name("  "), "area")
        p = output_path("out", "Desa Banaran", date(2024, 3, 1))
        self.assertTrue(p.endswith(os.path.join("Desa_Banaran", "NDVI_Desa_Banaran_2024-03.tif")))
        self.assertEqual(fmt_ha(12345.4), "12.345 ha")
        self.assertEqual(fmt_ha(44.12), "44,1 ha")


class CompositeTests(unittest.TestCase):
    def setUp(self):
        nan = np.nan
        self.stack = np.array([
            [[0.2, nan], [0.5, nan]],
            [[0.8, 0.4], [nan, nan]],
            [[0.5, 0.6], [0.7, nan]],
        ], dtype=np.float32)

    def test_median(self):
        out, count = composite(self.stack, "median")
        np.testing.assert_allclose(out[0], [0.5, 0.5])
        self.assertAlmostEqual(float(out[1, 0]), 0.6, places=6)
        self.assertTrue(np.isnan(out[1, 1]))
        self.assertEqual(count.tolist(), [[3, 2], [2, 0]])

    def test_max_and_mean(self):
        mvc, _ = composite(self.stack, "max")
        mean, _ = composite(self.stack, "mean")
        self.assertAlmostEqual(float(mvc[0, 0]), 0.8, places=6)
        self.assertAlmostEqual(float(mean[0, 0]), 0.5, places=6)


class GridTests(unittest.TestCase):
    def test_utm_zone(self):
        self.assertEqual(utm_epsg(110.4, -7.8), 32749)   # Yogyakarta
        self.assertEqual(utm_epsg(95.3, 5.5), 32646)     # Aceh
        self.assertEqual(utm_epsg(140.7, -2.5), 32754)   # Jayapura

    def test_grid_snapped_and_area(self):
        ag = build_area_grid(rect(107.300, -6.170, 107.306, -6.164), 10)
        g = ag.grid
        self.assertEqual(g.epsg, 32748)
        self.assertEqual(g.minx % 10, 0)
        self.assertEqual(g.maxy % 10, 0)
        self.assertAlmostEqual(ag.area_ha, 44.1, delta=0.5)
        ag.mask = polygon_mask(ag)
        self.assertAlmostEqual(ag.mask.sum() * 100 / 10_000, ag.area_ha, delta=1.0)

    def test_tiny_area_still_has_pixels(self):
        ag = build_area_grid(rect(110.0, -7.0, 110.00002, -6.99998), 10)
        self.assertGreater(polygon_mask(ag).sum(), 0)

    def test_write_geotiff_roundtrip(self):
        from osgeo import gdal

        ag = build_area_grid(rect(107.300, -6.170, 107.302, -6.168), 10)
        arr = np.full((ag.grid.height, ag.grid.width), 0.5, np.float32)
        arr[0, 0] = np.nan
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.tif")
            write_geotiff(path, arr, ag.grid, metadata={"MONTH": "2024-01"})
            ds = gdal.Open(path)
            band = ds.GetRasterBand(1)
            raw = band.ReadRaster(0, 0, 2, 1, buf_type=gdal.GDT_Float32)
            vals = np.frombuffer(raw, np.float32)
            self.assertEqual(band.GetNoDataValue(), NODATA)
            self.assertEqual(vals.tolist(), [NODATA, 0.5])
            self.assertEqual(ds.GetMetadataItem("MONTH"), "2024-01")
            ds = None


class AreaLimitTests(unittest.TestCase):
    def test_too_large_area_skipped_without_network(self):
        settings = Settings(out_dir=tempfile.gettempdir(), start_month=date(2024, 1, 1),
                            end_month=date(2024, 3, 1), max_area_ha=1000)
        res = process_area(Area("besar", rect(110.0, -7.9, 110.2, -7.7)), settings)
        self.assertEqual([r.status for r in res], ["too_large"] * 3)
        self.assertIn("melebihi batas", res[0].message)

    def test_report(self):
        res = [MonthResult("A", "2024-01", "ok", path="x.tif", n_scenes=3, coverage=0.9, ndvi_mean=0.61, area_ha=10),
               MonthResult("B", "2024-01", "too_large", area_ha=99999, message="terlalu besar")]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.csv")
            write_report(path, res)
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("berhasil", lines[1])
        self.assertIn("area terlalu besar", lines[2])


class RetryTests(unittest.TestCase):
    """Months without enough clear pixels fall back to scenes above the cloud limit."""

    def setUp(self):
        from types import SimpleNamespace

        import core.composite as comp

        self.comp = comp
        self.orig = (comp.search_scenes, comp.scene_ndvi_on_grid)

        def item(day, cloud):
            return SimpleNamespace(id=f"S2_{day}_{cloud}", date=day, cloud_cover=cloud, tile="T", footprint=None)

        # Jan: one clear scene. Feb: only a hazy scene under the limit (10% clear) and one
        # cloudy scene above it that is clear over the area. Mar: only scenes above the limit.
        self.items = [
            item(date(2024, 1, 10), 20),
            item(date(2024, 2, 5), 50), item(date(2024, 2, 20), 95),
            item(date(2024, 3, 3), 97),
        ]
        self.searched_cloud = []

        def fake_search(geom, start, end, max_cloud=60, **kw):
            self.searched_cloud.append(max_cloud)
            return [i for i in self.items if i.cloud_cover <= max_cloud]

        def fake_read(it, area_grid, valid_scl):
            arr = np.full(area_grid.mask.shape, 0.7, np.float32)
            if it.id == "S2_2024-02-05_50":
                arr[:] = np.nan
                arr.flat[: arr.size // 10] = 0.3
            return arr

        comp.search_scenes, comp.scene_ndvi_on_grid = fake_search, fake_read
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(out_dir=self.tmp.name, start_month=date(2024, 1, 1), end_month=date(2024, 3, 1),
                                 max_cloud=80, min_coverage=0.5, skip_existing=False, workers=1)
        self.area = Area("uji", rect(107.300, -6.170, 107.302, -6.168))

    def tearDown(self):
        self.comp.search_scenes, self.comp.scene_ndvi_on_grid = self.orig
        self.tmp.cleanup()

    def test_retry_rescues_cloudy_and_empty_months(self):
        res = process_area(self.area, self.settings)
        self.assertEqual(self.searched_cloud, [100])
        self.assertEqual([r.status for r in res], ["ok", "ok", "ok"])
        self.assertEqual([r.retried for r in res], [False, True, True])
        self.assertEqual(res[1].n_scenes, 2)
        self.assertIn("di atas batas awan", res[2].message)
        self.assertTrue(all(os.path.exists(r.path) for r in res))

    def test_without_retry_months_are_skipped(self):
        self.settings.retry_cloudy = False
        res = process_area(self.area, self.settings)
        self.assertEqual(self.searched_cloud, [80])
        self.assertEqual([r.status for r in res], ["ok", "low_coverage", "no_scenes"])
        self.assertFalse(any(r.retried for r in res))


class StacTests(unittest.TestCase):
    def test_jp2_only_item_skipped(self):
        asset = {"href": "s3://sentinel-s2-l2a/tiles/x/B04.jp2"}
        f = {"id": "S2B_48MYU_20221001_1_L2A",
             "properties": {"datetime": "2022-10-01T03:00:00Z", "proj:epsg": 32748},
             "assets": {"red": asset, "nir": asset, "scl": asset}}
        self.assertIsNone(parse_feature(f))


if __name__ == "__main__":
    unittest.main()
