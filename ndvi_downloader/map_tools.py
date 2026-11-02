"""Map tools to draw the area of interest on the canvas."""

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

AOI_STROKE = QColor(46, 125, 50)
AOI_FILL = QColor(102, 187, 106, 60)


def make_rubber_band(canvas):
    band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
    band.setStrokeColor(AOI_STROKE)
    band.setFillColor(AOI_FILL)
    band.setWidth(2)
    return band


class RectangleTool(QgsMapTool):
    """Click and drag to draw a rectangle."""

    geometryDrawn = pyqtSignal(QgsGeometry)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.band = make_rubber_band(canvas)
        self.start = None
        self.setCursor(Qt.CrossCursor)

    def canvasPressEvent(self, event):
        self.start = self.toMapCoordinates(event.pos())
        self.band.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event):
        if self.start is None:
            return
        end = self.toMapCoordinates(event.pos())
        self.band.setToGeometry(QgsGeometry.fromRect(QgsRectangle(self.start, end)), None)

    def canvasReleaseEvent(self, event):
        if self.start is None:
            return
        rect = QgsRectangle(self.start, self.toMapCoordinates(event.pos()))
        self.start = None
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        if rect.width() > 0 and rect.height() > 0:
            self.geometryDrawn.emit(QgsGeometry.fromRect(rect))

    def deactivate(self):
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()


class PolygonTool(QgsMapTool):
    """Left click adds vertices, right click (or double click) closes the polygon, Esc cancels."""

    geometryDrawn = pyqtSignal(QgsGeometry)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.band = make_rubber_band(canvas)
        self.points = []
        self.setCursor(Qt.CrossCursor)

    def _redraw(self, hover=None):
        pts = self.points + ([hover] if hover else [])
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        for p in pts:
            self.band.addPoint(p, False)
        self.band.show()
        self.band.updatePosition()
        self.canvas().refresh()

    def canvasPressEvent(self, event):
        point = QgsPointXY(self.toMapCoordinates(event.pos()))
        if event.button() == Qt.LeftButton:
            self.points.append(point)
            self._redraw()
        elif event.button() == Qt.RightButton:
            self._finish()

    def canvasDoubleClickEvent(self, event):
        self._finish()

    def canvasMoveEvent(self, event):
        if self.points:
            self._redraw(QgsPointXY(self.toMapCoordinates(event.pos())))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.points = []
            self.band.reset(QgsWkbTypes.PolygonGeometry)
        elif event.key() in (Qt.Key_Backspace, Qt.Key_Delete) and self.points:
            self.points.pop()
            self._redraw()

    def _finish(self):
        pts = list(self.points)
        self.points = []
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        if len(pts) >= 3:
            geom = QgsGeometry.fromPolygonXY([pts + [pts[0]]])
            if not geom.isGeosValid():
                geom = geom.makeValid()
            self.geometryDrawn.emit(geom)

    def deactivate(self):
        self.points = []
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()
