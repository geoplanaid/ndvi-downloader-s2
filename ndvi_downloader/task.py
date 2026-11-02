"""Background QgsTask that runs every area one after another."""

import traceback

from qgis.core import Qgis, QgsMessageLog, QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .core.composite import MonthResult, month_range, process_area

LOG_TAG = "NDVI Downloader"


class NdviDownloadTask(QgsTask):
    finishedWithResults = pyqtSignal(object)  # list of MonthResult
    logMessage = pyqtSignal(str)

    def __init__(self, areas, settings):
        super().__init__(f"NDVI bulanan Sentinel-2 ({len(areas)} area)", QgsTask.CanCancel)
        self.areas = areas
        self.settings = settings
        self.results = []

    def _log(self, msg):
        QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.Info)
        self.logMessage.emit(msg)

    def run(self):
        n = len(self.areas)
        for i, area in enumerate(self.areas):
            if self.isCanceled():
                break
            try:
                self.results.extend(process_area(
                    area, self.settings, log=self._log, is_canceled=self.isCanceled,
                    progress=lambda f, i=i: self.setProgress(100 * (i + f) / n),
                ))
            except Exception as exc:  # keep going with the next area
                QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.Critical)
                self._log(f"[{area.name}] ERROR: {exc}")
                self.results.extend(
                    MonthResult(area.name, f"{m:%Y-%m}", "error", message=str(exc))
                    for m in month_range(self.settings.start_month, self.settings.end_month)
                )
            self.setProgress(100 * (i + 1) / n)
        return True

    def finished(self, result):
        self.finishedWithResults.emit(self.results)
