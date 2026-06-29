from __future__ import annotations

import json
from pathlib import Path

import fitz
from PySide6.QtCore import QPoint, QPointF, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QGraphicsProxyWidget,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .graphics_items import AnnotationItem
from .models import (
    ARROW_KIND,
    ELLIPSE_KIND,
    LINE_KIND,
    RECT_KIND,
    TEXT_KIND,
    Annotation,
    annotations_from_dicts,
    annotations_to_dicts,
    load_edit_file,
    new_annotation_id,
    save_edit_file,
)
from .pdf_io import export_pdf


SELECT_TOOL = "select"
TOOL_LABELS = {
    SELECT_TOOL: "選択",
    TEXT_KIND: "文字",
    RECT_KIND: "四角",
    ELLIPSE_KIND: "楕円",
    LINE_KIND: "直線",
    ARROW_KIND: "矢印",
}


class ColorButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, text: str = "色", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._color = "#000000"
        self.clicked.connect(self._choose_color)
        self.setMinimumWidth(88)
        self.set_color(self._color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color or "#ffffff"
        swatch = self._color if QColor(self._color).isValid() else "#ffffff"
        self.setStyleSheet(
            "QPushButton {"
            f"background-color: {swatch};"
            "border: 1px solid #9aa0a6;"
            "padding: 4px 8px;"
            "}"
        )

    def _choose_color(self) -> None:
        initial = QColor(self._color)
        if not initial.isValid():
            initial = QColor("#000000")
        color = QColorDialog.getColor(initial, self, "色を選択")
        if color.isValid():
            value = color.name()
            self.set_color(value)
            self.colorChanged.emit(value)


class InlineTextEdit(QTextEdit):
    committed = Signal(str)
    canceled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._finished = False
        self.setAcceptRichText(False)
        self.setFrameShape(QTextEdit.Shape.NoFrame)
        self.setStyleSheet(
            "QTextEdit {"
            "background: rgba(255, 255, 255, 230);"
            "border: 1px solid #1a73e8;"
            "padding: 2px;"
            "}"
        )

    def commit(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.committed.emit(self.toPlainText())

    def cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.canceled.emit()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.commit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.commit()
            event.accept()
            return
        super().keyPressEvent(event)


class PdfGraphicsView(QGraphicsView):
    zoomRequested = Signal(int)
    contextMenuRequestedAt = Signal(QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRenderHints(self.renderHints())
        self.setBackgroundBrush(QColor("#f1f3f4"))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = 1 if event.angleDelta().y() > 0 else -1
            self.zoomRequested.emit(steps)
            event.accept()
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.contextMenuRequestedAt.emit(event.globalPos())


class PdfScene(QGraphicsScene):
    annotationCreated = Signal(object)
    annotationChanged = Signal(object)
    editTextRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_tool = SELECT_TOOL
        self.page_index = 0
        self.page_rect = QRectF()
        self.next_z = 1
        self.defaults = {
            "font_family": "Meiryo",
            "font_size": 14.0,
            "text_color": "#202124",
            "stroke_color": "#d93025",
            "stroke_width": 2.0,
            "fill_color": "#fff176",
            "fill_opacity": 0.0,
        }
        self._draft_item: AnnotationItem | None = None
        self._draft_start = QPointF()

    def set_tool(self, tool: str) -> None:
        self.current_tool = tool

    def set_page(self, page_index: int, page_rect: QRectF, next_z: int) -> None:
        self.page_index = page_index
        self.page_rect = page_rect
        self.next_z = next_z
        self.setSceneRect(page_rect)

    def add_annotation_item(self, annotation: Annotation) -> AnnotationItem:
        item = AnnotationItem(annotation)
        item.changed.connect(self.annotationChanged.emit)
        item.editTextRequested.connect(self.editTextRequested.emit)
        self.addItem(item)
        return item

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.page_rect.isNull():
            super().mousePressEvent(event)
            return

        pos = self._clamp_to_page(event.scenePos())
        if self.current_tool == TEXT_KIND and self.page_rect.contains(pos):
            annotation = self._make_annotation(TEXT_KIND, pos.x(), pos.y(), 180.0, 64.0)
            item = self.add_annotation_item(annotation)
            self.clearSelection()
            item.setSelected(True)
            self.annotationCreated.emit(annotation)
            self.editTextRequested.emit(annotation)
            event.accept()
            return

        if self.current_tool in {RECT_KIND, ELLIPSE_KIND, LINE_KIND, ARROW_KIND} and self.page_rect.contains(pos):
            self._draft_start = pos
            annotation = self._make_annotation(self.current_tool, pos.x(), pos.y(), 0.0, 0.0)
            self._draft_item = self.add_annotation_item(annotation)
            self.clearSelection()
            self._draft_item.setSelected(True)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._draft_item:
            self._update_draft(event.scenePos(), event.modifiers())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._draft_item:
            self._update_draft(event.scenePos(), event.modifiers())
            item = self._draft_item
            self._draft_item = None

            if self._draft_is_too_small(item.annotation):
                self.removeItem(item)
                item.deleteLater()
                event.accept()
                return

            item.annotation.z = self.next_z
            item.setZValue(item.annotation.z)
            self.next_z += 1
            self.annotationCreated.emit(item.annotation)
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _make_annotation(self, kind: str, x: float, y: float, width: float, height: float) -> Annotation:
        annotation = Annotation(
            id=new_annotation_id(),
            page=self.page_index,
            kind=kind,
            x=float(x),
            y=float(y),
            w=float(width),
            h=float(height),
            z=self.next_z,
        )
        annotation.font_family = self.defaults["font_family"]
        annotation.font_size = self.defaults["font_size"]
        annotation.text_color = self.defaults["text_color"]
        annotation.stroke_color = self.defaults["stroke_color"]
        annotation.stroke_width = self.defaults["stroke_width"]
        annotation.fill_color = self.defaults["fill_color"]
        annotation.fill_opacity = self.defaults["fill_opacity"]
        return annotation

    def _update_draft(self, scene_pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        if not self._draft_item:
            return
        pos = self._clamp_to_page(scene_pos)
        annotation = self._draft_item.annotation
        start = self._draft_start
        dx = pos.x() - start.x()
        dy = pos.y() - start.y()

        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            if annotation.kind in {RECT_KIND, ELLIPSE_KIND}:
                side = max(abs(dx), abs(dy))
                dx = side if dx >= 0 else -side
                dy = side if dy >= 0 else -side
            elif annotation.kind in {LINE_KIND, ARROW_KIND}:
                if abs(dx) >= abs(dy):
                    dy = 0.0
                else:
                    dx = 0.0

        self._draft_item.prepareGeometryChange()
        if annotation.kind in {RECT_KIND, ELLIPSE_KIND}:
            annotation.x = min(start.x(), start.x() + dx)
            annotation.y = min(start.y(), start.y() + dy)
            annotation.w = abs(dx)
            annotation.h = abs(dy)
        else:
            annotation.x = start.x()
            annotation.y = start.y()
            annotation.w = dx
            annotation.h = dy
        self._draft_item.sync_from_annotation()

    def _draft_is_too_small(self, annotation: Annotation) -> bool:
        if annotation.kind in {LINE_KIND, ARROW_KIND}:
            return abs(annotation.w) < 4.0 and abs(annotation.h) < 4.0
        return annotation.w < 4.0 or annotation.h < 4.0

    def _clamp_to_page(self, pos: QPointF) -> QPointF:
        if self.page_rect.isNull():
            return pos
        return QPointF(
            min(max(pos.x(), self.page_rect.left()), self.page_rect.right()),
            min(max(pos.y(), self.page_rect.top()), self.page_rect.bottom()),
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.pdf_doc: fitz.Document | None = None
        self.pdf_path: Path | None = None
        self.edit_path: Path | None = None
        self.annotations: list[Annotation] = []
        self.current_page = 0
        self.zoom = 1.0
        self.dirty = False

        self.history: list[str] = []
        self.history_index = -1
        self._restoring_history = False
        self._updating_properties = False
        self._clipboard_annotation: dict | None = None

        self._inline_proxy: QGraphicsProxyWidget | None = None
        self._inline_annotation_id: str | None = None
        self._inline_item: AnnotationItem | None = None

        self.scene = PdfScene(self)
        self.view = PdfGraphicsView(self)
        self.view.setScene(self.scene)
        self.setCentralWidget(self.view)

        self._create_actions()
        self._create_toolbar()
        self._create_page_dock()
        self._create_properties_dock()
        self._create_status_bar()
        self._connect_signals()

        self.setAcceptDrops(True)
        self._update_title()
        self._update_actions()
        self.statusBar().showMessage("PDFを開いてください")

    def closeEvent(self, event) -> None:
        if self._maybe_save():
            event.accept()
        else:
            event.ignore()

    def dragEnterEvent(self, event) -> None:
        if any(url.toLocalFile().lower().endswith((".pdf", ".pdfedit")) for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in {".pdf", ".pdfedit"}:
                self._open_path(path)
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def _create_actions(self) -> None:
        style = self.style()
        self.open_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "開く", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.open_file)

        self.save_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "保存", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.triggered.connect(self.save_edit)

        self.export_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon), "PDF出力", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+E"))
        self.export_action.triggered.connect(self.export_pdf)

        self.undo_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack), "元に戻す", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)

        self.redo_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward), "やり直す", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.redo)

        self.copy_action = QAction("コピー", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self.copy_selected)

        self.paste_action = QAction("貼り付け", self)
        self.paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        self.paste_action.triggered.connect(self.paste_annotation)

        self.delete_action = QAction("削除", self)
        self.delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        self.delete_action.triggered.connect(self.delete_selected)

        self.front_action = QAction("前面へ", self)
        self.front_action.triggered.connect(self.bring_to_front)

        self.back_action = QAction("背面へ", self)
        self.back_action.triggered.connect(self.send_to_back)

        self.zoom_in_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_ArrowUp), "拡大", self)
        self.zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.zoom_in_action.triggered.connect(lambda: self.change_zoom(1))

        self.zoom_out_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_ArrowDown), "縮小", self)
        self.zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.zoom_out_action.triggered.connect(lambda: self.change_zoom(-1))

        self.prev_page_action = QAction("前", self)
        self.prev_page_action.triggered.connect(lambda: self.go_to_page(self.current_page - 1))

        self.next_page_action = QAction("次", self)
        self.next_page_action.triggered.connect(lambda: self.go_to_page(self.current_page + 1))

        self.tool_group = QActionGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_actions: dict[str, QAction] = {}
        for tool, label in TOOL_LABELS.items():
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(tool)
            action.triggered.connect(self._tool_action_triggered)
            self.tool_group.addAction(action)
            self.tool_actions[tool] = action
        self.tool_actions[SELECT_TOOL].setChecked(True)

        self.addAction(self.copy_action)
        self.addAction(self.paste_action)
        self.addAction(self.delete_action)
        self.addAction(self.undo_action)
        self.addAction(self.redo_action)

    def _create_toolbar(self) -> None:
        toolbar = QToolBar("ツール", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        toolbar.addAction(self.open_action)
        toolbar.addAction(self.save_action)
        toolbar.addAction(self.export_action)
        toolbar.addSeparator()
        for tool in [SELECT_TOOL, TEXT_KIND, RECT_KIND, ELLIPSE_KIND, LINE_KIND, ARROW_KIND]:
            toolbar.addAction(self.tool_actions[tool])
        toolbar.addSeparator()
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)
        toolbar.addSeparator()
        toolbar.addAction(self.prev_page_action)
        self.page_spin = QSpinBox(self)
        self.page_spin.setMinimum(1)
        self.page_spin.setMaximum(1)
        self.page_spin.setFixedWidth(78)
        toolbar.addWidget(self.page_spin)
        toolbar.addAction(self.next_page_action)
        toolbar.addSeparator()
        toolbar.addAction(self.zoom_out_action)
        toolbar.addAction(self.zoom_in_action)

    def _create_page_dock(self) -> None:
        self.page_list = QListWidget(self)
        self.page_list.setMinimumWidth(96)
        self.page_list.setUniformItemSizes(True)
        dock = QDockWidget("ページ", self)
        dock.setObjectName("pageDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        dock.setWidget(self.page_list)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _create_properties_dock(self) -> None:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.no_selection_label = QLabel("オブジェクト未選択", panel)
        layout.addWidget(self.no_selection_label)

        self.common_group = QGroupBox("位置とサイズ", panel)
        common_form = QFormLayout(self.common_group)
        self.kind_label = QLabel("-", self.common_group)
        self.x_spin = self._make_double_spin()
        self.y_spin = self._make_double_spin()
        self.w_spin = self._make_double_spin()
        self.h_spin = self._make_double_spin()
        common_form.addRow("種類", self.kind_label)
        common_form.addRow("X", self.x_spin)
        common_form.addRow("Y", self.y_spin)
        common_form.addRow("幅", self.w_spin)
        common_form.addRow("高さ", self.h_spin)
        layout.addWidget(self.common_group)

        self.text_group = QGroupBox("文字", panel)
        text_form = QFormLayout(self.text_group)
        self.text_content = QTextEdit(self.text_group)
        self.text_content.setAcceptRichText(False)
        self.text_content.setFixedHeight(84)
        self.font_combo = QFontComboBox(self.text_group)
        self.font_size_spin = self._make_double_spin(1.0, 200.0, 14.0)
        self.text_color_button = ColorButton("文字色", self.text_group)
        self.text_bg_check = QCheckBox("背景", self.text_group)
        self.text_bg_button = ColorButton("背景色", self.text_group)
        self.text_border_check = QCheckBox("枠線", self.text_group)
        self.align_combo = QComboBox(self.text_group)
        self.align_combo.addItem("左", "left")
        self.align_combo.addItem("中央", "center")
        self.align_combo.addItem("右", "right")
        text_form.addRow("内容", self.text_content)
        text_form.addRow("フォント", self.font_combo)
        text_form.addRow("サイズ", self.font_size_spin)
        text_form.addRow("色", self.text_color_button)
        text_form.addRow("", self.text_bg_check)
        text_form.addRow("背景色", self.text_bg_button)
        text_form.addRow("", self.text_border_check)
        text_form.addRow("配置", self.align_combo)
        layout.addWidget(self.text_group)

        self.shape_group = QGroupBox("図形", panel)
        shape_form = QFormLayout(self.shape_group)
        self.stroke_color_button = ColorButton("線色", self.shape_group)
        self.stroke_width_spin = self._make_double_spin(0.1, 50.0, 2.0)
        self.fill_check = QCheckBox("塗りつぶし", self.shape_group)
        self.fill_color_button = ColorButton("塗り色", self.shape_group)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal, self.shape_group)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(0)
        shape_form.addRow("線色", self.stroke_color_button)
        shape_form.addRow("線幅", self.stroke_width_spin)
        shape_form.addRow("", self.fill_check)
        shape_form.addRow("塗り色", self.fill_color_button)
        shape_form.addRow("透明度", self.opacity_slider)
        layout.addWidget(self.shape_group)
        layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(260)

        dock = QDockWidget("設定", self)
        dock.setObjectName("propertiesDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        self.common_group.setEnabled(False)
        self.text_group.setVisible(False)
        self.shape_group.setVisible(False)

    def _create_status_bar(self) -> None:
        self.page_status_label = QLabel("ページ -/-", self)
        self.zoom_status_label = QLabel("100%", self)
        self.statusBar().addPermanentWidget(self.page_status_label)
        self.statusBar().addPermanentWidget(self.zoom_status_label)

    def _connect_signals(self) -> None:
        self.scene.annotationCreated.connect(self._annotation_created)
        self.scene.annotationChanged.connect(self._annotation_changed)
        self.scene.editTextRequested.connect(self.start_text_edit)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view.zoomRequested.connect(self.change_zoom)
        self.view.contextMenuRequestedAt.connect(self.show_context_menu)
        self.page_list.currentRowChanged.connect(self.go_to_page)
        self.page_spin.valueChanged.connect(lambda value: self.go_to_page(value - 1))

        for spin in [self.x_spin, self.y_spin, self.w_spin, self.h_spin]:
            spin.valueChanged.connect(self._geometry_changed)

        self.text_content.textChanged.connect(self._text_content_changed)
        self.font_combo.currentFontChanged.connect(self._font_changed)
        self.font_size_spin.valueChanged.connect(self._font_size_changed)
        self.text_color_button.colorChanged.connect(self._text_color_changed)
        self.text_bg_check.toggled.connect(self._text_background_toggled)
        self.text_bg_button.colorChanged.connect(self._text_background_changed)
        self.text_border_check.toggled.connect(self._text_border_toggled)
        self.align_combo.currentIndexChanged.connect(self._alignment_changed)

        self.stroke_color_button.colorChanged.connect(self._stroke_color_changed)
        self.stroke_width_spin.valueChanged.connect(self._stroke_width_changed)
        self.fill_check.toggled.connect(self._fill_toggled)
        self.fill_color_button.colorChanged.connect(self._fill_color_changed)
        self.opacity_slider.valueChanged.connect(self._opacity_changed)

    def _make_double_spin(self, minimum: float = -100000.0, maximum: float = 100000.0, value: float = 0.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setSingleStep(1.0)
        spin.setValue(value)
        spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return spin

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "PDFを開く",
            str(Path.home()),
            "PDF / 編集ファイル (*.pdf *.pdfedit);;PDF (*.pdf);;PDF編集ファイル (*.pdfedit)",
        )
        if path:
            self._open_path(Path(path))

    def _open_path(self, path: Path) -> None:
        if not self._maybe_save():
            return
        try:
            if path.suffix.lower() == ".pdfedit":
                self._open_edit_file(path)
            else:
                self._open_pdf(path)
        except Exception as exc:
            QMessageBox.critical(self, "読み込みエラー", str(exc))

    def _open_pdf(self, path: Path) -> None:
        self._close_document()
        self.pdf_doc = fitz.open(str(path))
        self.pdf_path = path.resolve()
        self.edit_path = None
        self.annotations = []
        self.current_page = 0
        self.zoom = 1.0
        self._reset_history(mark_dirty=False)
        self._populate_page_list()
        self._render_current_page()
        self.statusBar().showMessage(f"開きました: {self.pdf_path.name}", 4000)
        self.dirty = False
        self._update_title()
        self._update_actions()

    def _open_edit_file(self, path: Path) -> None:
        source_pdf, annotations = load_edit_file(path)
        source_path = Path(source_pdf)
        if not source_path.exists():
            replacement, _ = QFileDialog.getOpenFileName(
                self,
                "元PDFを選択",
                str(path.parent),
                "PDF (*.pdf)",
            )
            if not replacement:
                raise FileNotFoundError(f"元PDFが見つかりません: {source_path}")
            source_path = Path(replacement)

        self._close_document()
        self.pdf_doc = fitz.open(str(source_path))
        self.pdf_path = source_path.resolve()
        self.edit_path = path.resolve()
        self.annotations = annotations
        self.current_page = 0
        self.zoom = 1.0
        self._reset_history(mark_dirty=False)
        self._populate_page_list()
        self._render_current_page()
        self.statusBar().showMessage(f"編集ファイルを開きました: {self.edit_path.name}", 4000)
        self.dirty = False
        self._update_title()
        self._update_actions()

    def save_edit(self) -> bool:
        if not self._has_document():
            return False
        self._commit_inline_editor()
        if self.edit_path is None:
            default_path = self.pdf_path.with_suffix(".pdfedit") if self.pdf_path else Path.home() / "untitled.pdfedit"
            path, _ = QFileDialog.getSaveFileName(
                self,
                "編集状態を保存",
                str(default_path),
                "PDF編集ファイル (*.pdfedit)",
            )
            if not path:
                return False
            self.edit_path = Path(path)
        try:
            save_edit_file(self.edit_path, self.pdf_path, self.annotations)
        except Exception as exc:
            QMessageBox.critical(self, "保存エラー", str(exc))
            return False
        self.dirty = False
        self.statusBar().showMessage(f"保存しました: {self.edit_path.name}", 4000)
        self._update_title()
        self._update_actions()
        return True

    def export_pdf(self) -> None:
        if not self._has_document():
            return
        self._commit_inline_editor()
        default_path = self.pdf_path.with_name(f"{self.pdf_path.stem}_edited.pdf") if self.pdf_path else Path.home() / "edited.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "PDFとして出力",
            str(default_path),
            "PDF (*.pdf)",
        )
        if not path:
            return
        try:
            export_pdf(self.pdf_path, path, self.annotations)
        except Exception as exc:
            QMessageBox.critical(self, "PDF出力エラー", str(exc))
            return
        self.statusBar().showMessage(f"PDFを出力しました: {Path(path).name}", 5000)

    def go_to_page(self, page_index: int) -> None:
        if not self._has_document():
            return
        if page_index < 0 or page_index >= self.pdf_doc.page_count:
            self._sync_page_controls()
            return
        if page_index == self.current_page and self.scene.items():
            self._sync_page_controls()
            return
        self._commit_inline_editor()
        self.current_page = page_index
        self._render_current_page()

    def change_zoom(self, steps: int) -> None:
        if not self._has_document():
            return
        factor = 1.15 ** steps
        self.zoom = max(0.25, min(4.0, self.zoom * factor))
        self._render_current_page()

    def copy_selected(self) -> None:
        item = self._selected_item()
        if not item:
            return
        self._clipboard_annotation = item.annotation.to_dict()
        self._update_actions()

    def paste_annotation(self) -> None:
        if not self._has_document() or not self._clipboard_annotation:
            return
        data = dict(self._clipboard_annotation)
        data["id"] = new_annotation_id()
        data["page"] = self.current_page
        data["x"] = float(data.get("x", 0.0)) + 16.0
        data["y"] = float(data.get("y", 0.0)) + 16.0
        data["z"] = self._next_z()
        annotation = Annotation.from_dict(data)
        self.annotations.append(annotation)
        item = self.scene.add_annotation_item(annotation)
        self.scene.clearSelection()
        item.setSelected(True)
        self._record_history()
        self._populate_page_list()

    def delete_selected(self) -> None:
        item = self._selected_item()
        if not item:
            return
        self._delete_annotation(item.annotation.id, record=True)

    def bring_to_front(self) -> None:
        item = self._selected_item()
        if not item:
            return
        item.annotation.z = self._next_z()
        item.sync_from_annotation()
        self._record_history()

    def send_to_back(self) -> None:
        item = self._selected_item()
        if not item:
            return
        min_z = min((annotation.z for annotation in self.annotations), default=0)
        item.annotation.z = min_z - 1
        item.sync_from_annotation()
        self._record_history()

    def undo(self) -> None:
        if self.history_index <= 0:
            return
        self._commit_inline_editor()
        self.history_index -= 1
        self._restore_history()

    def redo(self) -> None:
        if self.history_index >= len(self.history) - 1:
            return
        self._commit_inline_editor()
        self.history_index += 1
        self._restore_history()

    def show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction(self.copy_action)
        menu.addAction(self.paste_action)
        menu.addAction(self.delete_action)
        menu.addSeparator()
        menu.addAction(self.front_action)
        menu.addAction(self.back_action)
        self._update_actions()
        menu.exec(global_pos)

    def start_text_edit(self, annotation: Annotation) -> None:
        if annotation.page != self.current_page:
            return
        self._commit_inline_editor()
        item = self._item_for_annotation(annotation.id)
        if not item:
            return

        editor = InlineTextEdit()
        editor.setPlainText(annotation.text)
        editor.setFont(QFont(annotation.font_family, max(1, int(annotation.font_size))))
        editor.selectAll()
        editor.committed.connect(lambda text, annotation_id=annotation.id: self._finish_text_edit(annotation_id, text))
        editor.canceled.connect(lambda annotation_id=annotation.id: self._cancel_text_edit(annotation_id))

        proxy = self.scene.addWidget(editor)
        proxy.setZValue(item.zValue() + 1)
        proxy.setPos(annotation.x, annotation.y)
        proxy.resize(max(120.0, annotation.w), max(40.0, annotation.h))

        item.setVisible(False)
        self._inline_proxy = proxy
        self._inline_annotation_id = annotation.id
        self._inline_item = item
        QTimer.singleShot(0, editor.setFocus)

    def _finish_text_edit(self, annotation_id: str, text: str) -> None:
        annotation = self._annotation_by_id(annotation_id)
        self._remove_inline_editor()
        if not annotation:
            return
        if not text.strip():
            self._delete_annotation(annotation_id, record=True)
            return
        annotation.text = text
        item = self._item_for_annotation(annotation_id)
        if item:
            item.setVisible(True)
            item.update()
            item.setSelected(True)
        self._record_history()
        self._selection_changed()

    def _cancel_text_edit(self, annotation_id: str) -> None:
        annotation = self._annotation_by_id(annotation_id)
        self._remove_inline_editor()
        if self._inline_item:
            self._inline_item.setVisible(True)
        if annotation and not annotation.text.strip():
            self._delete_annotation(annotation_id, record=True)

    def _remove_inline_editor(self) -> None:
        if self._inline_item:
            self._inline_item.setVisible(True)
        if self._inline_proxy:
            widget = self._inline_proxy.widget()
            self.scene.removeItem(self._inline_proxy)
            if widget:
                widget.deleteLater()
            self._inline_proxy.deleteLater()
        self._inline_proxy = None
        self._inline_annotation_id = None
        self._inline_item = None

    def _commit_inline_editor(self) -> None:
        if self._inline_proxy and self._inline_proxy.widget():
            widget = self._inline_proxy.widget()
            if isinstance(widget, InlineTextEdit):
                widget.commit()

    def _annotation_created(self, annotation: Annotation) -> None:
        self.annotations.append(annotation)
        self._record_history()
        self._populate_page_list()
        self._update_actions()

    def _annotation_changed(self, annotation: Annotation) -> None:
        self._record_history()
        self._populate_page_list()
        self._selection_changed()

    def _selection_changed(self) -> None:
        self._update_properties_from_selection()
        self._update_actions()

    def _render_current_page(self) -> None:
        if not self._has_document():
            self.scene.clear()
            return

        self.scene.clear()
        page = self.pdf_doc[self.current_page]
        page_rect = QRectF(0.0, 0.0, float(page.rect.width), float(page.rect.height))
        render_scale = max(1.0, self.zoom)
        pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)

        background = self.scene.addPixmap(pixmap)
        background.setScale(1.0 / render_scale)
        background.setZValue(-10000)
        self.scene.set_page(self.current_page, page_rect, self._next_z())

        for annotation in sorted(self._annotations_for_current_page(), key=lambda item: item.z):
            self.scene.add_annotation_item(annotation)

        self.view.setTransform(QTransform().scale(self.zoom, self.zoom))
        self._sync_page_controls()
        self._update_status()
        self._update_properties_from_selection()

    def _populate_page_list(self) -> None:
        if not self._has_document():
            self.page_list.clear()
            return
        blockers = [QSignalBlocker(self.page_list)]
        edited_pages = {annotation.page for annotation in self.annotations}
        self.page_list.clear()
        for index in range(self.pdf_doc.page_count):
            mark = " *" if index in edited_pages else ""
            self.page_list.addItem(f"{index + 1}{mark}")
        self.page_list.setCurrentRow(self.current_page)
        blockers.clear()

    def _sync_page_controls(self) -> None:
        if not self._has_document():
            self.page_spin.setMaximum(1)
            self.page_spin.setValue(1)
            self.page_status_label.setText("ページ -/-")
            return
        blockers = [QSignalBlocker(self.page_spin), QSignalBlocker(self.page_list)]
        self.page_spin.setMaximum(max(1, self.pdf_doc.page_count))
        self.page_spin.setValue(self.current_page + 1)
        self.page_list.setCurrentRow(self.current_page)
        blockers.clear()

    def _update_status(self) -> None:
        if self._has_document():
            self.page_status_label.setText(f"ページ {self.current_page + 1}/{self.pdf_doc.page_count}")
        else:
            self.page_status_label.setText("ページ -/-")
        self.zoom_status_label.setText(f"{int(self.zoom * 100)}%")

    def _update_properties_from_selection(self) -> None:
        item = self._selected_item()
        self._updating_properties = True
        widgets = [
            self.x_spin,
            self.y_spin,
            self.w_spin,
            self.h_spin,
            self.text_content,
            self.font_combo,
            self.font_size_spin,
            self.text_color_button,
            self.text_bg_check,
            self.text_bg_button,
            self.text_border_check,
            self.align_combo,
            self.stroke_color_button,
            self.stroke_width_spin,
            self.fill_check,
            self.fill_color_button,
            self.opacity_slider,
        ]
        blockers = [QSignalBlocker(widget) for widget in widgets]
        try:
            has_item = item is not None
            self.no_selection_label.setVisible(not has_item)
            self.common_group.setEnabled(has_item)
            self.text_group.setVisible(False)
            self.shape_group.setVisible(False)
            if not item:
                return

            annotation = item.annotation
            self.kind_label.setText(TOOL_LABELS.get(annotation.kind, annotation.kind))
            self.x_spin.setValue(annotation.x)
            self.y_spin.setValue(annotation.y)
            self.w_spin.setValue(annotation.w)
            self.h_spin.setValue(annotation.h)

            if annotation.kind == TEXT_KIND:
                self.text_group.setVisible(True)
                self.text_content.setPlainText(annotation.text)
                self.font_combo.setCurrentFont(QFont(annotation.font_family))
                self.font_size_spin.setValue(annotation.font_size)
                self.text_color_button.set_color(annotation.text_color)
                self.text_bg_check.setChecked(bool(annotation.background_color))
                self.text_bg_button.set_color(annotation.background_color or "#ffffff")
                self.text_border_check.setChecked(annotation.border_enabled)
                index = self.align_combo.findData(annotation.alignment)
                self.align_combo.setCurrentIndex(max(0, index))
            else:
                self.shape_group.setVisible(True)
                self.stroke_color_button.set_color(annotation.stroke_color)
                self.stroke_width_spin.setValue(annotation.stroke_width)
                self.fill_check.setChecked(annotation.fill_opacity > 0.0)
                self.fill_color_button.set_color(annotation.fill_color)
                self.opacity_slider.setValue(int(annotation.fill_opacity * 100))
        finally:
            blockers.clear()
            self._updating_properties = False

    def _geometry_changed(self) -> None:
        item = self._selected_item()
        if self._updating_properties or not item:
            return
        annotation = item.annotation
        annotation.x = self.x_spin.value()
        annotation.y = self.y_spin.value()
        annotation.w = self.w_spin.value()
        annotation.h = self.h_spin.value()
        item.sync_from_annotation()
        self._record_history()

    def _text_content_changed(self) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        item.annotation.text = self.text_content.toPlainText()
        item.update()
        self._record_history()

    def _font_changed(self, font: QFont) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        item.annotation.font_family = font.family()
        item.update()
        self.scene.defaults["font_family"] = font.family()
        self._record_history()

    def _font_size_changed(self, value: float) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        item.annotation.font_size = value
        item.update()
        self.scene.defaults["font_size"] = value
        self._record_history()

    def _text_color_changed(self, color: str) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        item.annotation.text_color = color
        item.update()
        self.scene.defaults["text_color"] = color
        self._record_history()

    def _text_background_toggled(self, checked: bool) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        item.annotation.background_color = self.text_bg_button.color() if checked else ""
        item.update()
        self._record_history()

    def _text_background_changed(self, color: str) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        if self.text_bg_check.isChecked():
            item.annotation.background_color = color
            item.update()
            self._record_history()

    def _text_border_toggled(self, checked: bool) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        item.annotation.border_enabled = checked
        item.update()
        self._record_history()

    def _alignment_changed(self) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind != TEXT_KIND:
            return
        item.annotation.alignment = self.align_combo.currentData()
        item.update()
        self._record_history()

    def _stroke_color_changed(self, color: str) -> None:
        item = self._selected_item()
        if self._updating_properties or not item:
            return
        item.annotation.stroke_color = color
        item.update()
        self.scene.defaults["stroke_color"] = color
        self._record_history()

    def _stroke_width_changed(self, value: float) -> None:
        item = self._selected_item()
        if self._updating_properties or not item:
            return
        item.annotation.stroke_width = value
        item.prepareGeometryChange()
        item.update()
        self.scene.defaults["stroke_width"] = value
        self._record_history()

    def _fill_toggled(self, checked: bool) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind == TEXT_KIND:
            return
        item.annotation.fill_opacity = max(0.2, self.opacity_slider.value() / 100.0) if checked else 0.0
        self.opacity_slider.setValue(int(item.annotation.fill_opacity * 100))
        item.update()
        self.scene.defaults["fill_opacity"] = item.annotation.fill_opacity
        self._record_history()

    def _fill_color_changed(self, color: str) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind == TEXT_KIND:
            return
        item.annotation.fill_color = color
        item.update()
        self.scene.defaults["fill_color"] = color
        self._record_history()

    def _opacity_changed(self, value: int) -> None:
        item = self._selected_item()
        if self._updating_properties or not item or item.annotation.kind == TEXT_KIND:
            return
        item.annotation.fill_opacity = value / 100.0
        self.fill_check.setChecked(value > 0)
        item.update()
        self.scene.defaults["fill_opacity"] = item.annotation.fill_opacity
        self._record_history()

    def _tool_action_triggered(self) -> None:
        action = self.sender()
        if isinstance(action, QAction):
            self.scene.set_tool(action.data())

    def _selected_item(self) -> AnnotationItem | None:
        for item in self.scene.selectedItems():
            if isinstance(item, AnnotationItem):
                return item
        return None

    def _item_for_annotation(self, annotation_id: str) -> AnnotationItem | None:
        for item in self.scene.items():
            if isinstance(item, AnnotationItem) and item.annotation.id == annotation_id:
                return item
        return None

    def _annotation_by_id(self, annotation_id: str) -> Annotation | None:
        for annotation in self.annotations:
            if annotation.id == annotation_id:
                return annotation
        return None

    def _delete_annotation(self, annotation_id: str, *, record: bool) -> None:
        item = self._item_for_annotation(annotation_id)
        if item:
            self.scene.removeItem(item)
            item.deleteLater()
        self.annotations = [annotation for annotation in self.annotations if annotation.id != annotation_id]
        self._populate_page_list()
        self._selection_changed()
        if record:
            self._record_history()

    def _annotations_for_current_page(self) -> list[Annotation]:
        return [annotation for annotation in self.annotations if annotation.page == self.current_page]

    def _next_z(self) -> int:
        return max((annotation.z for annotation in self.annotations), default=0) + 1

    def _snapshot(self) -> str:
        return json.dumps(annotations_to_dicts(self.annotations), ensure_ascii=False, sort_keys=True)

    def _reset_history(self, *, mark_dirty: bool) -> None:
        self.history = []
        self.history_index = -1
        self._record_history(mark_dirty=mark_dirty)

    def _record_history(self, *, mark_dirty: bool = True) -> None:
        if self._restoring_history:
            return
        snapshot = self._snapshot()
        if self.history_index >= 0 and self.history[self.history_index] == snapshot:
            return
        if self.history_index < len(self.history) - 1:
            self.history = self.history[: self.history_index + 1]
        self.history.append(snapshot)
        if len(self.history) > 100:
            self.history.pop(0)
        self.history_index = len(self.history) - 1
        if mark_dirty:
            self.dirty = True
        self._update_title()
        self._update_actions()

    def _restore_history(self) -> None:
        if self.history_index < 0 or self.history_index >= len(self.history):
            return
        self._restoring_history = True
        try:
            self.annotations = annotations_from_dicts(json.loads(self.history[self.history_index]))
            self._populate_page_list()
            self._render_current_page()
            self.dirty = True
        finally:
            self._restoring_history = False
        self._update_title()
        self._update_actions()

    def _maybe_save(self) -> bool:
        if not self.dirty:
            return True
        result = QMessageBox.question(
            self,
            "未保存の編集",
            "編集状態を保存しますか？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if result == QMessageBox.StandardButton.Save:
            return self.save_edit()
        if result == QMessageBox.StandardButton.Discard:
            return True
        return False

    def _close_document(self) -> None:
        self._commit_inline_editor()
        if self.pdf_doc:
            self.pdf_doc.close()
        self.pdf_doc = None
        self.pdf_path = None
        self.edit_path = None
        self.annotations = []
        self.scene.clear()

    def _has_document(self) -> bool:
        return self.pdf_doc is not None and self.pdf_path is not None

    def _update_title(self) -> None:
        name = self.pdf_path.name if self.pdf_path else "Simple PDF Editor"
        marker = " *" if self.dirty else ""
        self.setWindowTitle(f"{name}{marker} - Simple PDF Editor")

    def _update_actions(self) -> None:
        has_document = self._has_document()
        has_selection = self._selected_item() is not None
        self.save_action.setEnabled(has_document)
        self.export_action.setEnabled(has_document)
        self.prev_page_action.setEnabled(has_document and self.current_page > 0)
        self.next_page_action.setEnabled(has_document and self.pdf_doc is not None and self.current_page < self.pdf_doc.page_count - 1)
        self.copy_action.setEnabled(has_selection)
        self.delete_action.setEnabled(has_selection)
        self.front_action.setEnabled(has_selection)
        self.back_action.setEnabled(has_selection)
        self.paste_action.setEnabled(has_document and self._clipboard_annotation is not None)
        self.undo_action.setEnabled(self.history_index > 0)
        self.redo_action.setEnabled(self.history_index < len(self.history) - 1)


def run() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1280, 860)
    window.show()
    return app.exec()
