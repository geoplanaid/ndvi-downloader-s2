"""Download monthly NDVI for a few test areas without opening QGIS.

    python-qgis-ltr.bat scripts/smoke_test.py [out_dir] [start YYYY-MM] [end YYYY-MM]
"""

import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ndvi_downloader"))

from core.composite import Area, Settings, process_area, write_report  # noqa: E402


def rect(minx, miny, maxx, maxy):
    return {"type": "Polygon", "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]]}


args = sys.argv[1:]
out_dir = args[0] if args else os.path.join(os.environ.get("TEMP", "/tmp"), "ndvi_downloader_smoke")
start = date.fromisoformat((args[1] if len(args) > 1 else "2024-06") + "-01")
end = date.fromisoformat((args[2] if len(args) > 2 else "2024-08") + "-01")

areas = [
    Area("Sawah Karawang", rect(107.300, -6.170, 107.306, -6.164)),
    Area("IKN", rect(116.6930, -0.9730, 116.6990, -0.9680)),
    Area("Terlalu besar", rect(110.0, -7.9, 110.6, -7.4)),  # ~3.700 km2, must be skipped
]
settings = Settings(out_dir=out_dir, start_month=start, end_month=end, max_area_ha=10_000, skip_existing=False)

t0 = time.time()
results = []
for area in areas:
    results.extend(process_area(area, settings, log=print))
write_report(os.path.join(out_dir, "ringkasan.csv"), results)
print(f"\nSelesai dalam {time.time() - t0:.0f} s")
for r in results:
    print(f"  {r.area:15s} {r.month}  {r.status:13s} cov={r.coverage:4.0%}  ndvi={r.ndvi_mean:6.3f}  {r.message}")
