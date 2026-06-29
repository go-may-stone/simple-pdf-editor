from __future__ import annotations

from math import atan2, cos, sin
from pathlib import Path

import fitz
from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QImage, QPainter, QPen, QTextOption

from .models import ARROW_KIND, ELLIPSE_KIND, LINE_KIND, RECT_KIND, TEXT_KIND, Annotation


_QT_APP: QGuiApplication | None = None


def export_pdf(source_pdf: str | Path, output_pdf: str | Path, annotations: list[Annotation]) -> None:
    source_path = Path(source_pdf).resolve()
    output_path = Path(output_pdf).resolve()
    if source_path == output_path:
        raise ValueError("Output PDF must be different from the source PDF.")

    grouped: dict[int, list[Annotation]] = {}
    for annotation in annotations:
        grouped.setdefault(annotation.page, []).append(annotation)

    doc = fitz.open(str(source_path))
    try:
        for page_index, page_annotations in grouped.items():
            if page_index < 0 or page_index >= doc.page_count:
                continue
            page = doc[page_index]
            for annotation in sorted(page_annotations, key=lambda item: item.z):
                _draw_annotation(page, annotation)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_path), garbage=4, deflate=True)
    finally:
        doc.close()


def _draw_annotation(page: fitz.Page, annotation: Annotation) -> None:
    if annotation.kind == TEXT_KIND:
        _draw_text(page, annotation)
    elif annotation.kind == RECT_KIND:
        _draw_rect(page, annotation)
    elif annotation.kind == ELLIPSE_KIND:
        _draw_ellipse(page, annotation)
    elif annotation.kind == LINE_KIND:
        _draw_line(page, annotation, arrow=False)
    elif annotation.kind == ARROW_KIND:
        _draw_line(page, annotation, arrow=True)


def _draw_text(page: fitz.Page, annotation: Annotation) -> None:
    rect = fitz.Rect(
        annotation.x,
        annotation.y,
        annotation.x + max(1.0, annotation.w),
        annotation.y + max(1.0, annotation.h),
    )
    stream = _render_text_annotation_png(annotation)
    page.insert_image(rect, stream=stream, overlay=True, keep_proportion=False)


def _draw_rect(page: fitz.Page, annotation: Annotation) -> None:
    rect = _normalized_rect(annotation)
    page.draw_rect(
        rect,
        color=_rgb(annotation.stroke_color),
        fill=_fill(annotation),
        width=max(0.1, annotation.stroke_width),
        fill_opacity=_fill_opacity(annotation),
        overlay=True,
    )


def _draw_ellipse(page: fitz.Page, annotation: Annotation) -> None:
    rect = _normalized_rect(annotation)
    page.draw_oval(
        rect,
        color=_rgb(annotation.stroke_color),
        fill=_fill(annotation),
        width=max(0.1, annotation.stroke_width),
        fill_opacity=_fill_opacity(annotation),
        overlay=True,
    )


def _draw_line(page: fitz.Page, annotation: Annotation, *, arrow: bool) -> None:
    start = fitz.Point(annotation.x, annotation.y)
    end = fitz.Point(annotation.x + annotation.w, annotation.y + annotation.h)
    color = _rgb(annotation.stroke_color) or (0, 0, 0)
    width = max(0.1, annotation.stroke_width)
    page.draw_line(start, end, color=color, width=width, overlay=True)

    if arrow:
        _draw_arrow_head(page, start, end, color, width)


def _draw_arrow_head(page: fitz.Page, start: fitz.Point, end: fitz.Point, color: tuple[float, float, float], width: float) -> None:
    dx = end.x - start.x
    dy = end.y - start.y
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return

    angle = atan2(dy, dx)
    length = max(12.0, width * 5.0)
    spread = 0.55
    p1 = fitz.Point(end.x - length * cos(angle - spread), end.y - length * sin(angle - spread))
    p2 = fitz.Point(end.x - length * cos(angle + spread), end.y - length * sin(angle + spread))
    page.draw_line(end, p1, color=color, width=width, overlay=True)
    page.draw_line(end, p2, color=color, width=width, overlay=True)


def _normalized_rect(annotation: Annotation) -> fitz.Rect:
    x1 = annotation.x
    y1 = annotation.y
    x2 = annotation.x + annotation.w
    y2 = annotation.y + annotation.h
    return fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _rgb(value: str) -> tuple[float, float, float] | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
    if len(value) == 8:
        value = value[-6:]
    if len(value) != 6:
        return None
    try:
        red = int(value[0:2], 16) / 255.0
        green = int(value[2:4], 16) / 255.0
        blue = int(value[4:6], 16) / 255.0
    except ValueError:
        return None
    return (red, green, blue)


def _fill(annotation: Annotation) -> tuple[float, float, float] | None:
    if annotation.fill_opacity <= 0:
        return None
    return _rgb(annotation.fill_color)


def _fill_opacity(annotation: Annotation) -> float:
    if annotation.fill_opacity <= 0:
        return 1.0
    return max(0.0, min(1.0, annotation.fill_opacity))


def _render_text_annotation_png(annotation: Annotation) -> bytes:
    _ensure_qt_app()
    width = max(1.0, float(annotation.w))
    height = max(1.0, float(annotation.h))
    scale = 3.0
    image = QImage(
        max(1, int(width * scale)),
        max(1, int(height * scale)),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(QColor(0, 0, 0, 0))

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.scale(scale, scale)

        local_rect = QRectF(0.0, 0.0, width, height)
        background = _qt_color(annotation.background_color)
        if background.alpha() > 0:
            painter.fillRect(local_rect, QBrush(background))

        if annotation.border_enabled:
            painter.setPen(QPen(_qt_color(annotation.stroke_color), max(0.1, annotation.stroke_width)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(local_rect)

        font = QFont(annotation.font_family)
        font.setPointSizeF(max(1.0, annotation.font_size))
        painter.setFont(font)
        painter.setPen(QPen(_qt_color(annotation.text_color)))

        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        option.setAlignment(_qt_alignment(annotation.alignment))
        painter.drawText(local_rect.adjusted(4.0, 2.0, -4.0, -2.0), annotation.text, option)
    finally:
        painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _ensure_qt_app() -> None:
    global _QT_APP
    if QGuiApplication.instance() is None:
        _QT_APP = QGuiApplication([])


def _qt_color(value: str) -> QColor:
    if not value:
        return QColor(0, 0, 0, 0)
    color = QColor(value)
    if not color.isValid():
        return QColor(0, 0, 0, 255)
    return color


def _qt_alignment(alignment: str) -> Qt.AlignmentFlag:
    vertical = Qt.AlignmentFlag.AlignTop
    if alignment == "center":
        return Qt.AlignmentFlag.AlignHCenter | vertical
    if alignment == "right":
        return Qt.AlignmentFlag.AlignRight | vertical
    return Qt.AlignmentFlag.AlignLeft | vertical
