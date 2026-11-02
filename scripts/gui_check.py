"""Headless end-to-end check of the plugin UI against live data.

Builds a polygon layer with three features (two small, one larger than the
limit), runs the real QgsTask and saves a screenshot of the panel.

    python-qgis-ltr.bat scripts/gui_check.py [out_dir] [screenshot_dir]
"""

import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
if os.name != "nt":  # Windows renders fonts properly with its native platform
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QDate, Qt, QVariant  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

app = QgsApplication([], True)
app.initQgis()

out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("TEMP", "/tmp"), "ndvi_downloader_gui")
shot_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "docs")
os.makedirs(shot_dir, exist_ok=True)


class MessageBar:
    def pushSuccess(self, title, text):
        print(f"[{title}] {text}")


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.bar = MessageBar()

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def __getattr__(self, name):
        return lambda *a, **k: None


layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Lahan uji", "memory")
layer.dataProvider().addAttributes([QgsField("nama", QVariant.String)])
layer.updateFields()
for name, bbox in [
    ("Sawah Karawang", (107.300, -6.170, 107.306, -6.164)),
    ("IKN", (116.6930, -0.9730, 116.6990, -0.9680)),
    ("Jogja Raya", (110.0, -7.9, 110.6, -7.4)),
]:
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromRect(QgsRectangle(*bbox)))
    f["nama"] = name
    layer.dataProvider().addFeature(f)
QgsProject.instance().addMapLayer(layer)

iface = Iface()
from ndvi_downloader import classFactory  # noqa: E402
from ndvi_downloader import dock as dock_module  # noqa: E402

dock_module.NdviDownloaderDock._warn = lambda self, text: print("WARNING DIALOG:", text, flush=True)
plugin = classFactory(iface)
plugin.initGui()
plugin.action.setChecked(True)
dock = plugin.dock

dock.layer_combo.setLayer(layer)
dock.field_combo.setField("nama")
dock.chk_selected.setChecked(False)
dock.spin_max_ha.setValue(10_000)
dock.chk_skip.setChecked(False)
dock.chk_add.setChecked(True)
dock.out_dir.setFilePath(out_dir)
dock.month_start.setDate(QDate(2024, 6, 1))
dock.month_end.setDate(QDate(2024, 8, 1))
dock._areas_from_layer()
print("Areas:", dock.lbl_areas.text())
print("Estimate:", dock.lbl_estimate.text())

t0 = time.time()
dock._run()
last = 0
while dock.task is not None:
    app.processEvents()
    time.sleep(0.05)
    if time.time() - last > 15:
        last = time.time()
        print(f"  {time.time() - t0:5.0f} s  {dock.progress.value():3d}%", flush=True)
print(f"Task done in {time.time() - t0:.0f} s")
print(dock.log.toPlainText())

layers = sorted(l.name() for l in QgsProject.instance().mapLayers().values() if l.name().startswith("NDVI"))
print("Layers:", layers)

dock.setAttribute(Qt.WA_DontShowOnScreen, True)
dock.resize(460, 1120)
dock.show()
app.processEvents()
dock.widget().widget().grab().save(os.path.join(shot_dir, "panel.png"))
plugin.unload()
print("Screenshot:", os.path.join(shot_dir, "panel.png"))
app.exitQgis()
