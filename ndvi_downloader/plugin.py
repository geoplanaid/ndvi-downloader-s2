"""QGIS plugin entry: menu entry, toolbar button and the dock panel."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

MENU = "&NDVI Downloader Sentinel-2"


class NdviDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        self.action = QAction(icon, "NDVI Downloader Bulanan Sentinel-2", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip("Unduh GeoTIFF NDVI Sentinel-2 per bulan untuk area yang dipilih")
        self.action.toggled.connect(self._toggle_dock)
        self.iface.addRasterToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu(MENU, self.action)

    def _toggle_dock(self, checked):
        if self.dock is None:
            from .dock import NdviDownloaderDock  # imported lazily so QGIS starts fast

            self.dock = NdviDownloaderDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)
        self.dock.setVisible(checked)

    def unload(self):
        if self.dock is not None:
            self.dock.cleanup()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginRasterMenu(MENU, self.action)
            self.iface.removeRasterToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
