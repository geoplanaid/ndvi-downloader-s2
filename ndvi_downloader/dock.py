"""Dock panel: pick areas and months, download monthly NDVI GeoTIFFs."""

import json
import os
from collections import Counter
from datetime import date, datetime

from qgis.core import (
    QgsApplication,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFieldProxyModel,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSettings,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
    QgsWkbTypes,
)
from qgis.gui import QgsFieldComboBox, QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QDate, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDockWidget,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .core.composite import Area, Settings, fmt_ha, month_range, write_report
from .core.raster import DEFAULT_VALID_SCL
from .map_tools import PolygonTool, RectangleTool, make_rubber_band
from .task import NdviDownloadTask

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
SETTINGS_PREFIX = "geoplana_ndvi_downloader/"
MAX_FEATURES = 500
METHODS = [("Median", "median"), ("Maksimum (MVC)", "max"), ("Rata-rata", "mean")]
RESOLUTIONS = [10, 20, 30, 60]
TOO_LARGE_COLOR = QColor(198, 40, 40)


class NdviDownloaderDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("NDVI Downloader Bulanan Sentinel-2", parent)
        self.setObjectName("GeoplanaNdviDownloaderDock")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.areas = []  # list of (Area, hectares)
        self.band_ok = make_rubber_band(self.canvas)
        self.band_big = make_rubber_band(self.canvas)
        self.band_big.setStrokeColor(TOO_LARGE_COLOR)
        self.band_big.setFillColor(QColor(198, 40, 40, 50))
        self.task = None
        self.prev_tool = None
        self.rect_tool = RectangleTool(self.canvas)
        self.poly_tool = PolygonTool(self.canvas)
        self.rect_tool.geometryDrawn.connect(self._on_drawn)
        self.poly_tool.geometryDrawn.connect(self._on_drawn)
        self.da = QgsDistanceArea()
        self.da.setSourceCrs(WGS84, QgsProject.instance().transformContext())
        self.da.setEllipsoid("WGS84")

        self._loading = True
        self._build_ui()
        self._load_settings()
        self._loading = False
        self._update_summary()
        self.canvas.destinationCrsChanged.connect(self._show_areas)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        body = QWidget()
        root = QVBoxLayout(body)

        box_area = QGroupBox("1. Area")
        la = QVBoxLayout(box_area)
        row = QHBoxLayout()
        self.btn_rect = QPushButton("Gambar kotak")
        self.btn_poly = QPushButton("Gambar poligon")
        self.btn_clear = QPushButton("Hapus")
        self.btn_poly.setToolTip("Klik kiri menambah titik, klik kanan atau dobel klik untuk selesai, Esc untuk batal")
        for b in (self.btn_rect, self.btn_poly, self.btn_clear):
            row.addWidget(b)
        la.addLayout(row)
        form = QFormLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.field_combo = QgsFieldComboBox()
        self.field_combo.setAllowEmptyFieldName(True)
        self.field_combo.setFilters(QgsFieldProxyModel.AllTypes)
        self.field_combo.setLayer(self.layer_combo.currentLayer())
        form.addRow("Layer poligon", self.layer_combo)
        form.addRow("Kolom nama area", self.field_combo)
        la.addLayout(form)
        row = QHBoxLayout()
        self.chk_selected = QCheckBox("Fitur terpilih saja")
        self.chk_selected.setChecked(True)
        self.btn_layer = QPushButton("Pakai layer")
        row.addWidget(self.chk_selected)
        row.addStretch()
        row.addWidget(self.btn_layer)
        la.addLayout(row)
        self.lbl_areas = QLabel("Belum ada area.")
        self.lbl_areas.setWordWrap(True)
        la.addWidget(self.lbl_areas)
        root.addWidget(box_area)

        box_time = QGroupBox("2. Bulan && filter awan")
        lt = QFormLayout(box_time)
        today = QDate.currentDate()
        last_month = QDate(today.year(), today.month(), 1).addMonths(-1)
        self.month_start = QDateEdit(last_month.addMonths(-11))
        self.month_end = QDateEdit(last_month)
        for d in (self.month_start, self.month_end):
            d.setDisplayFormat("MM-yyyy")
            d.setCalendarPopup(True)
            d.setMinimumDate(QDate(2017, 1, 1))
            d.setMaximumDate(today)
            d.setMinimumWidth(95)
        row = QHBoxLayout()
        row.addWidget(self.month_start)
        row.addWidget(QLabel("s/d"))
        row.addWidget(self.month_end)
        row.addStretch()
        lt.addRow("Bulan", row)
        self.spin_cloud = QSpinBox()
        self.spin_cloud.setRange(0, 100)
        self.spin_cloud.setSuffix(" %")
        self.spin_cloud.setToolTip("Batas tutupan awan seluruh scene (100 x 100 km). Awan di area tetap dimasker per piksel.")
        lt.addRow("Awan scene maks", self.spin_cloud)
        self.spin_cov = QSpinBox()
        self.spin_cov.setRange(0, 100)
        self.spin_cov.setSuffix(" %")
        self.spin_cov.setToolTip("Bulan dilewati jika bagian area yang bebas awan kurang dari ini")
        lt.addRow("Area bebas awan min", self.spin_cov)
        self.chk_water = QCheckBox("Sertakan piksel air (sawah tergenang, tambak)")
        lt.addRow(self.chk_water)
        root.addWidget(box_time)

        box_out = QGroupBox("3. Output")
        lo = QFormLayout(box_out)
        self.out_dir = QgsFileWidget()
        self.out_dir.setStorageMode(QgsFileWidget.GetDirectory)
        lo.addRow("Folder", self.out_dir)
        self.cmb_method = QComboBox()
        for label, key in METHODS:
            self.cmb_method.addItem(label, key)
        self.cmb_method.setToolTip("Median: paling tahan sisa awan. Maksimum (MVC): kehijauan puncak bulan itu.")
        lo.addRow("Komposit", self.cmb_method)
        self.cmb_res = QComboBox()
        for r in RESOLUTIONS:
            self.cmb_res.addItem(f"{r} m", r)
        lo.addRow("Resolusi", self.cmb_res)
        self.spin_max_ha = QSpinBox()
        self.spin_max_ha.setRange(1, 1_000_000)
        self.spin_max_ha.setSingleStep(1000)
        self.spin_max_ha.setSuffix(" ha")
        self.spin_max_ha.setToolTip("Area yang lebih luas dari ini langsung dilewati")
        lo.addRow("Luas maks per area", self.spin_max_ha)
        self.chk_skip = QCheckBox("Lewati bulan yang filenya sudah ada")
        self.chk_add = QCheckBox("Tambahkan hasil ke peta")
        lo.addRow(self.chk_skip)
        lo.addRow(self.chk_add)
        root.addWidget(box_out)

        self.lbl_estimate = QLabel("")
        self.lbl_estimate.setWordWrap(True)
        self.lbl_estimate.setStyleSheet("color:#2e7d32;font-weight:bold")
        root.addWidget(self.lbl_estimate)
        row = QHBoxLayout()
        self.btn_run = QPushButton("Unduh NDVI")
        self.btn_run.setStyleSheet("QPushButton{background:#2e7d32;color:white;font-weight:bold;padding:6px}")
        self.btn_cancel = QPushButton("Batal")
        self.btn_cancel.setEnabled(False)
        self.btn_open = QPushButton("Buka folder")
        row.addWidget(self.btn_run, 1)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_open)
        root.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        root.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(170)
        self.log.setPlaceholderText("Log proses akan muncul di sini")
        root.addWidget(self.log)
        credit = QLabel('Data: Copernicus Sentinel-2 L2A via <a href="https://earth-search.aws.element84.com/v1">Earth Search</a> &middot; '
                        '<a href="https://geoplanaid.github.io">Geoplana</a>')
        credit.setOpenExternalLinks(True)
        credit.setStyleSheet("color:#888;font-size:10px")
        root.addWidget(credit)
        root.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.setWidget(scroll)

        self.btn_rect.clicked.connect(lambda: self._activate_tool(self.rect_tool))
        self.btn_poly.clicked.connect(lambda: self._activate_tool(self.poly_tool))
        self.btn_clear.clicked.connect(self._clear_areas)
        self.layer_combo.layerChanged.connect(self.field_combo.setLayer)
        self.btn_layer.clicked.connect(self._areas_from_layer)
        self.btn_run.clicked.connect(self._run)
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_open.clicked.connect(self._open_folder)
        self.spin_max_ha.valueChanged.connect(self._update_summary)
        self.month_start.dateChanged.connect(self._update_summary)
        self.month_end.dateChanged.connect(self._update_summary)

    def _load_settings(self):
        s = QgsSettings()
        self.spin_cloud.setValue(int(s.value(SETTINGS_PREFIX + "max_cloud", 80)))
        self.spin_cov.setValue(int(s.value(SETTINGS_PREFIX + "min_coverage", 50)))
        self.spin_max_ha.setValue(int(s.value(SETTINGS_PREFIX + "max_area_ha", 10_000)))
        self.chk_water.setChecked(s.value(SETTINGS_PREFIX + "water", True, type=bool))
        self.chk_skip.setChecked(s.value(SETTINGS_PREFIX + "skip_existing", True, type=bool))
        self.chk_add.setChecked(s.value(SETTINGS_PREFIX + "add_to_map", True, type=bool))
        self.cmb_method.setCurrentIndex(int(s.value(SETTINGS_PREFIX + "method", 0)))
        self.cmb_res.setCurrentIndex(int(s.value(SETTINGS_PREFIX + "resolution", 0)))
        self.out_dir.setFilePath(s.value(SETTINGS_PREFIX + "out_dir",
                                         os.path.join(os.path.expanduser("~"), "NDVI_Sentinel2")))

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS_PREFIX + "max_cloud", self.spin_cloud.value())
        s.setValue(SETTINGS_PREFIX + "min_coverage", self.spin_cov.value())
        s.setValue(SETTINGS_PREFIX + "max_area_ha", self.spin_max_ha.value())
        s.setValue(SETTINGS_PREFIX + "water", self.chk_water.isChecked())
        s.setValue(SETTINGS_PREFIX + "skip_existing", self.chk_skip.isChecked())
        s.setValue(SETTINGS_PREFIX + "add_to_map", self.chk_add.isChecked())
        s.setValue(SETTINGS_PREFIX + "method", self.cmb_method.currentIndex())
        s.setValue(SETTINGS_PREFIX + "resolution", self.cmb_res.currentIndex())
        s.setValue(SETTINGS_PREFIX + "out_dir", self.out_dir.filePath())

    # ------------------------------------------------------------------ Areas
    def _activate_tool(self, tool):
        current = self.canvas.mapTool()
        if current not in (self.rect_tool, self.poly_tool):
            self.prev_tool = current
        self.canvas.setMapTool(tool)

    def _restore_tool(self):
        if self.canvas.mapTool() in (self.rect_tool, self.poly_tool):
            if self.prev_tool is not None:
                self.canvas.setMapTool(self.prev_tool)
            else:
                self.canvas.unsetMapTool(self.canvas.mapTool())

    def _to_wgs84(self, geom, crs):
        geom = QgsGeometry(geom)
        if crs != WGS84:
            geom.transform(QgsCoordinateTransform(crs, WGS84, QgsProject.instance()))
        if not geom.isGeosValid():
            geom = geom.makeValid()
        if sum(1 for _ in geom.vertices()) > 2000:
            geom = geom.simplify(0.00002)
        return geom

    def _make_area(self, name, geom_wgs84):
        return Area(name=name, geojson=json.loads(geom_wgs84.asJson())), self.da.measureArea(geom_wgs84) / 10_000

    def _on_drawn(self, geom):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        g = self._to_wgs84(geom, canvas_crs)
        self.areas = [self._make_area("area", g)]
        self._restore_tool()
        self._show_areas()
        self._update_summary()

    def _areas_from_layer(self):
        layer = self.layer_combo.currentLayer()
        if layer is None:
            self._warn("Pilih layer poligon terlebih dahulu.")
            return
        feats = layer.selectedFeatures() if self.chk_selected.isChecked() else list(layer.getFeatures())
        if self.chk_selected.isChecked() and not feats:
            self._warn("Tidak ada fitur terpilih. Pilih fitur, atau hilangkan centang 'Fitur terpilih saja'.")
            return
        if len(feats) > MAX_FEATURES:
            self._warn(f"Terlalu banyak fitur ({len(feats)}). Maksimum {MAX_FEATURES} area sekali jalan.")
            return
        field = self.field_combo.currentField()
        areas, used = [], Counter()
        for f in feats:
            if not f.hasGeometry() or f.geometry().type() != QgsWkbTypes.PolygonGeometry:
                continue
            raw = f[field] if field else None
            name = str(raw) if raw not in (None, "") and str(raw) != "NULL" else f"fitur_{f.id()}"
            used[name] += 1
            if used[name] > 1:  # keep file names unique
                name = f"{name}_{f.id()}"
            areas.append(self._make_area(name, self._to_wgs84(f.geometry(), layer.crs())))
        if not areas:
            self._warn("Layer tidak memiliki geometri poligon.")
            return
        self.areas = areas
        self._show_areas()
        self._update_summary()

    def _show_areas(self):
        self.band_ok.reset(QgsWkbTypes.PolygonGeometry)
        self.band_big.reset(QgsWkbTypes.PolygonGeometry)
        limit = self.spin_max_ha.value()
        for area, ha in self.areas:
            geom = QgsGeometry.fromWkt(_geojson_to_wkt(area.geojson))
            (self.band_big if ha > limit else self.band_ok).addGeometry(geom, WGS84)

    def _clear_areas(self):
        self.areas = []
        self._show_areas()
        self._restore_tool()
        self._update_summary()

    def _months(self):
        a = self.month_start.date()
        b = self.month_end.date()
        return month_range(date(a.year(), a.month(), 1), date(b.year(), b.month(), 1))

    def _update_summary(self):
        if self._loading:
            return
        self._show_areas()
        limit = self.spin_max_ha.value()
        n_months = len(self._months())
        if not self.areas:
            self.lbl_areas.setText("Belum ada area.")
            self.lbl_estimate.setText("")
            return
        ok = [(a, ha) for a, ha in self.areas if ha <= limit]
        big = [(a, ha) for a, ha in self.areas if ha > limit]
        total = sum(ha for _, ha in ok)
        text = f"{len(self.areas)} area, total {fmt_ha(sum(ha for _, ha in self.areas))}."
        if len(self.areas) == 1:
            text = f"Area: {fmt_ha(self.areas[0][1])}."
        if big:
            names = ", ".join(a.name for a, _ in big[:5]) + (" ..." if len(big) > 5 else "")
            text += (f"<br><span style='color:#c62828'><b>{len(big)} area melebihi {fmt_ha(limit)} "
                     f"dan akan dilewati</b> (garis merah): {names}</span>")
        self.lbl_areas.setText(text)
        if ok and n_months:
            self.lbl_estimate.setText(f"Akan dibuat hingga {len(ok) * n_months} GeoTIFF "
                                      f"({len(ok)} area x {n_months} bulan, {fmt_ha(total)}).")
        else:
            self.lbl_estimate.setText("Tidak ada area yang bisa diproses." if not ok else "Rentang bulan kosong.")

    # ------------------------------------------------------------------ Run
    def _run(self):
        if not self.areas:
            self._warn("Gambar area atau pilih layer poligon terlebih dahulu.")
            return
        months = self._months()
        if not months:
            self._warn("Bulan mulai harus sebelum atau sama dengan bulan akhir.")
            return
        out_dir = self.out_dir.filePath().strip()
        if not out_dir:
            self._warn("Pilih folder output.")
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            self._warn(f"Folder output tidak bisa dibuat: {exc}")
            return
        if all(ha > self.spin_max_ha.value() for _, ha in self.areas):
            self._warn("Semua area melebihi batas luas maksimum. Perkecil area atau naikkan batasnya.")
            return

        self._save_settings()
        valid_scl = list(DEFAULT_VALID_SCL)
        if not self.chk_water.isChecked():
            valid_scl.remove(6)
        settings = Settings(
            out_dir=out_dir,
            start_month=months[0],
            end_month=months[-1],
            resolution=float(self.cmb_res.currentData()),
            method=self.cmb_method.currentData(),
            max_cloud=self.spin_cloud.value(),
            min_coverage=self.spin_cov.value() / 100,
            max_area_ha=self.spin_max_ha.value(),
            valid_scl=tuple(valid_scl),
            skip_existing=self.chk_skip.isChecked(),
        )
        self.task = NdviDownloadTask([a for a, _ in self.areas], settings)
        self.task.progressChanged.connect(lambda v: self.progress.setValue(int(v)))
        self.task.logMessage.connect(self.log.appendPlainText)
        self.task.finishedWithResults.connect(self._on_finished)
        self.run_settings = settings
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self.log.appendPlainText(f"=== Mulai {datetime.now():%H:%M:%S}: {len(self.areas)} area, "
                                 f"{months[0]:%m-%Y} s/d {months[-1]:%m-%Y} ===")
        QgsApplication.taskManager().addTask(self.task)

    def _cancel(self):
        if self.task is not None:
            self.task.cancel()
            self.log.appendPlainText("Membatalkan...")

    def _on_finished(self, results):
        self.task = None
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setValue(100)
        settings = self.run_settings
        report = os.path.join(settings.out_dir, f"ringkasan_ndvi_{datetime.now():%Y%m%d_%H%M%S}.csv")
        try:
            write_report(report, results)
        except OSError as exc:
            report = f"(gagal ditulis: {exc})"
        counts = Counter(r.status for r in results)
        skipped_areas = sorted({r.area for r in results if r.status == "too_large"})
        lines = [
            f"=== Selesai: {counts['ok']} GeoTIFF dibuat",
            f"    sudah ada: {counts['exists']}, terlalu berawan: {counts['low_coverage']}, "
            f"tidak ada citra: {counts['no_scenes']}, error: {counts['error']}, dibatalkan: {counts['canceled']}",
        ]
        if skipped_areas:
            lines.append(f"    area dilewati karena terlalu besar: {', '.join(skipped_areas)}")
        lines.append(f"    ringkasan: {report}")
        self.log.appendPlainText("\n".join(lines))
        if self.chk_add.isChecked():
            self._add_layers([r for r in results if r.status in ("ok", "exists") and r.path])
        self.iface.messageBar().pushSuccess("NDVI Downloader", f"{counts['ok']} GeoTIFF NDVI bulanan dibuat di {settings.out_dir}")

    def _add_layers(self, results):
        if not results:
            return
        root = QgsProject.instance().layerTreeRoot()
        parent = root.findGroup("NDVI bulanan") or root.insertGroup(0, "NDVI bulanan")
        for r in sorted(results, key=lambda r: (r.area, r.month), reverse=True):
            group = parent.findGroup(r.area) or parent.addGroup(r.area)
            name = f"NDVI {r.area} {r.month}"
            existing = [l for l in QgsProject.instance().mapLayersByName(name)]
            for layer in existing:
                QgsProject.instance().removeMapLayer(layer.id())
            layer = QgsRasterLayer(r.path, name, "gdal")
            if not layer.isValid():
                continue
            style_ndvi(layer)
            QgsProject.instance().addMapLayer(layer, False)
            group.addLayer(layer)
        # Show only the latest month of each area to keep the map readable
        for group in parent.findGroups():
            children = group.findLayers()
            for i, node in enumerate(sorted(children, key=lambda n: n.name(), reverse=True)):
                node.setItemVisibilityChecked(i == 0)

    def _open_folder(self):
        path = self.out_dir.filePath()
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------ Helpers
    def _warn(self, text):
        QMessageBox.warning(self, "NDVI Downloader", text)

    def cleanup(self):
        if self.task is not None:
            self.task.cancel()
        self._restore_tool()
        for band in (self.band_ok, self.band_big):
            band.reset(QgsWkbTypes.PolygonGeometry)
            self.canvas.scene().removeItem(band)
        try:
            self.canvas.destinationCrsChanged.disconnect(self._show_areas)
        except TypeError:
            pass

    def closeEvent(self, event):
        self._restore_tool()
        super().closeEvent(event)


def style_ndvi(layer, vmin=-0.1, vmax=0.9):
    """Red-yellow-green pseudocolor between vmin and vmax."""
    ramp = QgsStyle.defaultStyle().colorRamp("RdYlGn")
    shader_fn = QgsColorRampShader(vmin, vmax, ramp)
    shader_fn.setColorRampType(QgsColorRampShader.Interpolated)
    shader_fn.classifyColorRamp(9)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(shader_fn)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(vmin)
    renderer.setClassificationMax(vmax)
    layer.setRenderer(renderer)


def _geojson_to_wkt(geojson):
    from osgeo import ogr

    return ogr.CreateGeometryFromJson(json.dumps(geojson)).ExportToWkt()
