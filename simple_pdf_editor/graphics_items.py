from __future__ import annotations

from math import atan2, cos, sin

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QTextOption,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from .models import ARROW_KIND, ELLIPSE_KIND, LINE_KIND, RECT_KIND, TEXT_KIND, Annotation


HANDLE_SIZE = 8.0
MIN_OBJECT_SIZE = 8.0


def qcolor(value: str, opacity: float = 1.0) -> QColor:
    if not value:
        return QColor(0, 0, 0, 0)
    color = QColor(value)
    if not color.isValid():
        color = QColor("#000000")
    color.setAlphaF(max(0.0, min(1.0, opacity)))
    return color


class AnnotationItem(QGraphicsObject):
    changed = Signal(object)
    editTextRequested = Signal(object)

    def __init__(self, annotation: Annotation, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self.annotation = annotation
        self._active_handle: str | None = None
        self._press_scene_pos = QPointF()
        self._press_geometry = (annotation.x, annotation.y, annotation.w, annotation.h)
        self._press_item_pos = QPointF(annotation.x, annotation.y)

        self.setPos(annotation.x, annotation.y)
        self.setZValue(annotation.z)
        self.setAcceptHoverEvents(True)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )

    def boundingRect(self) -> QRectF:
        pad = max(HANDLE_SIZE, self.annotation.stroke_width + 4.0)
        if self.annotation.kind in {LINE_KIND, ARROW_KIND}:
            left = min(0.0, self.annotation.w)
            right = max(0.0, self.annotation.w)
            top = min(0.0, self.annotation.h)
            bottom = max(0.0, self.annotation.h)
            if right - left < 1.0:
                right += 1.0
            if bottom - top < 1.0:
                bottom += 1.0
            return QRectF(left - pad, top - pad, right - left + pad * 2.0, bottom - top + pad * 2.0)

        return QRectF(
            -pad,
            -pad,
            max(MIN_OBJECT_SIZE, self.annotation.w) + pad * 2.0,
            max(MIN_OBJECT_SIZE, self.annotation.h) + pad * 2.0,
        )

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        kind = self.annotation.kind
        if kind == TEXT_KIND:
            self._paint_text(painter)
        elif kind == RECT_KIND:
            self._paint_rect(painter)
        elif kind == ELLIPSE_KIND:
            self._paint_ellipse(painter)
        elif kind in {LINE_KIND, ARROW_KIND}:
            self._paint_line(painter, arrow=kind == ARROW_KIND)

        if self.isSelected():
            self._paint_selection(painter)

    def request_text_edit(self) -> None:
        if self.annotation.kind == TEXT_KIND:
            self.editTextRequested.emit(self.annotation)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.annotation.kind == TEXT_KIND and event.button() == Qt.MouseButton.LeftButton:
            self.editTextRequested.emit(self.annotation)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSelected():
            handle = self._handle_at(event.pos())
            if handle:
                self._active_handle = handle
                self._press_scene_pos = event.scenePos()
                self._press_geometry = (
                    self.annotation.x,
                    self.annotation.y,
                    self.annotation.w,
                    self.annotation.h,
                )
                event.accept()
                return

        self._press_item_pos = self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._active_handle:
            self._resize_from_handle(event.scenePos() - self._press_scene_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._active_handle:
            self._active_handle = None
            self.changed.emit(self.annotation)
            event.accept()
            return

        super().mouseReleaseEvent(event)
        if self.pos() != self._press_item_pos:
            self.changed.emit(self.annotation)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.annotation.x = float(value.x())
            self.annotation.y = float(value.y())
        return super().itemChange(change, value)

    def sync_from_annotation(self) -> None:
        self.prepareGeometryChange()
        self.setPos(self.annotation.x, self.annotation.y)
        self.setZValue(self.annotation.z)
        self.update()

    def _paint_text(self, painter: QPainter) -> None:
        rect = QRectF(0, 0, max(MIN_OBJECT_SIZE, self.annotation.w), max(MIN_OBJECT_SIZE, self.annotation.h))
        background = qcolor(self.annotation.background_color)
        if background.alpha() > 0:
            painter.fillRect(rect, QBrush(background))

        if self.annotation.border_enabled:
            painter.setPen(self._stroke_pen())
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)

        font = QFont(self.annotation.font_family)
        font.setPointSizeF(max(1.0, self.annotation.font_size))
        painter.setFont(font)
        painter.setPen(QPen(qcolor(self.annotation.text_color)))

        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        option.setAlignment(self._text_alignment())
        painter.drawText(rect.adjusted(4.0, 2.0, -4.0, -2.0), self.annotation.text, option)

    def _paint_rect(self, painter: QPainter) -> None:
        rect = QRectF(0, 0, max(MIN_OBJECT_SIZE, self.annotation.w), max(MIN_OBJECT_SIZE, self.annotation.h))
        painter.setPen(self._stroke_pen())
        painter.setBrush(self._fill_brush())
        painter.drawRect(rect)

    def _paint_ellipse(self, painter: QPainter) -> None:
        rect = QRectF(0, 0, max(MIN_OBJECT_SIZE, self.annotation.w), max(MIN_OBJECT_SIZE, self.annotation.h))
        painter.setPen(self._stroke_pen())
        painter.setBrush(self._fill_brush())
        painter.drawEllipse(rect)

    def _paint_line(self, painter: QPainter, *, arrow: bool) -> None:
        start = QPointF(0.0, 0.0)
        end = QPointF(self.annotation.w, self.annotation.h)
        painter.setPen(self._stroke_pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(start, end)
        if arrow:
            self._paint_arrow_head(painter, start, end)

    def _paint_arrow_head(self, painter: QPainter, start: QPointF, end: QPointF) -> None:
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        if abs(dx) < 0.01 and abs(dy) < 0.01:
            return

        angle = atan2(dy, dx)
        length = max(12.0, self.annotation.stroke_width * 5.0)
        spread = 0.55
        p1 = QPointF(end.x() - length * cos(angle - spread), end.y() - length * sin(angle - spread))
        p2 = QPointF(end.x() - length * cos(angle + spread), end.y() - length * sin(angle + spread))

        path = QPainterPath(end)
        path.lineTo(p1)
        path.lineTo(p2)
        path.closeSubpath()
        painter.setBrush(QBrush(qcolor(self.annotation.stroke_color)))
        painter.drawPath(path)

    def _paint_selection(self, painter: QPainter) -> None:
        painter.save()
        selection_pen = QPen(QColor("#1a73e8"), 0, Qt.PenStyle.DashLine)
        painter.setPen(selection_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self.annotation.kind in {LINE_KIND, ARROW_KIND}:
            painter.drawRect(self._line_bounds().adjusted(-3.0, -3.0, 3.0, 3.0))
        else:
            painter.drawRect(QRectF(0, 0, max(MIN_OBJECT_SIZE, self.annotation.w), max(MIN_OBJECT_SIZE, self.annotation.h)))

        handle_pen = QPen(QColor("#1a73e8"), 0)
        painter.setPen(handle_pen)
        painter.setBrush(QBrush(QColor("#ffffff")))
        for point in self._handle_points().values():
            painter.drawRect(self._handle_rect(point))
        painter.restore()

    def _stroke_pen(self) -> QPen:
        pen = QPen(qcolor(self.annotation.stroke_color), max(0.1, self.annotation.stroke_width))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen

    def _fill_brush(self) -> QBrush:
        if self.annotation.fill_opacity <= 0.0:
            return QBrush(Qt.BrushStyle.NoBrush)
        return QBrush(qcolor(self.annotation.fill_color, self.annotation.fill_opacity))

    def _text_alignment(self) -> Qt.AlignmentFlag:
        vertical = Qt.AlignmentFlag.AlignTop
        if self.annotation.alignment == "center":
            return Qt.AlignmentFlag.AlignHCenter | vertical
        if self.annotation.alignment == "right":
            return Qt.AlignmentFlag.AlignRight | vertical
        return Qt.AlignmentFlag.AlignLeft | vertical

    def _line_bounds(self) -> QRectF:
        return QRectF(
            min(0.0, self.annotation.w),
            min(0.0, self.annotation.h),
            abs(self.annotation.w),
            abs(self.annotation.h),
        )

    def _handle_points(self) -> dict[str, QPointF]:
        if self.annotation.kind in {LINE_KIND, ARROW_KIND}:
            return {
                "line_start": QPointF(0.0, 0.0),
                "line_end": QPointF(self.annotation.w, self.annotation.h),
            }

        width = max(MIN_OBJECT_SIZE, self.annotation.w)
        height = max(MIN_OBJECT_SIZE, self.annotation.h)
        return {
            "top_left": QPointF(0.0, 0.0),
            "top_right": QPointF(width, 0.0),
            "bottom_right": QPointF(width, height),
            "bottom_left": QPointF(0.0, height),
        }

    def _handle_rect(self, point: QPointF) -> QRectF:
        half = HANDLE_SIZE / 2.0
        return QRectF(point.x() - half, point.y() - half, HANDLE_SIZE, HANDLE_SIZE)

    def _handle_at(self, pos: QPointF) -> str | None:
        for name, point in self._handle_points().items():
            if self._handle_rect(point).contains(pos):
                return name
        return None

    def _resize_from_handle(self, delta: QPointF) -> None:
        x, y, width, height = self._press_geometry
        dx = float(delta.x())
        dy = float(delta.y())

        self.prepareGeometryChange()
        if self.annotation.kind in {LINE_KIND, ARROW_KIND}:
            if self._active_handle == "line_start":
                self.annotation.x = x + dx
                self.annotation.y = y + dy
                self.annotation.w = width - dx
                self.annotation.h = height - dy
                self.setPos(self.annotation.x, self.annotation.y)
            elif self._active_handle == "line_end":
                self.annotation.w = width + dx
                self.annotation.h = height + dy
            self.update()
            return

        new_x = x
        new_y = y
        new_w = width
        new_h = height

        if self._active_handle in {"top_left", "bottom_left"}:
            new_x = min(x + dx, x + width - MIN_OBJECT_SIZE)
            new_w = width - (new_x - x)
        elif self._active_handle in {"top_right", "bottom_right"}:
            new_w = max(MIN_OBJECT_SIZE, width + dx)

        if self._active_handle in {"top_left", "top_right"}:
            new_y = min(y + dy, y + height - MIN_OBJECT_SIZE)
            new_h = height - (new_y - y)
        elif self._active_handle in {"bottom_left", "bottom_right"}:
            new_h = max(MIN_OBJECT_SIZE, height + dy)

        self.annotation.x = new_x
        self.annotation.y = new_y
        self.annotation.w = max(MIN_OBJECT_SIZE, new_w)
        self.annotation.h = max(MIN_OBJECT_SIZE, new_h)
        self.setPos(self.annotation.x, self.annotation.y)
        self.update()
