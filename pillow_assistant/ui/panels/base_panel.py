"""Base class for type-adaptive file panels (R2).

A FilePanel is a dark, semi-transparent window with a type-specific preview on
top and a shared input area below (model picker + prompt + streamed answer).
Dropped paths are added to the session as references, so a question asked here
carries the file(s) to the Agent. Subclasses implement ``build_preview``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QRect, Qt

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.acrylic import disable_acrylic, enable_acrylic, glass_opacity, glass_theme_changed, white_acrylic_color
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


class _CornerGrip(QWidget):
    """A visible, reliable bottom-right resize handle (resizes via setGeometry)."""

    SIZE = 20

    def __init__(self, panel: "FilePanel", parent=None) -> None:
        super().__init__(parent if parent is not None else panel)
        self.panel = panel
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.SizeFDiagCursor)
        self._start_geo = None
        self._start_mouse = None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._start_geo = QRect(self.panel.geometry())
            self._start_mouse = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._start_geo is not None:
            d = event.globalPosition().toPoint() - self._start_mouse
            g = QRect(self._start_geo)
            g.setWidth(max(self.panel.minimumWidth(), g.width() + d.x()))
            g.setHeight(max(self.panel.minimumHeight(), g.height() + d.y()))
            self.panel.setGeometry(g)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._start_geo = None

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(55, 65, 78, 130), 1.5))
        s = self.SIZE
        for off in (5, 10, 15):
            p.drawLine(s - off, s - 4, s - 4, s - off)
        p.end()

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType, RequestKind

def panel_qss(opacity: int) -> str:
    alpha = round(opacity * 2.55)
    control_alpha = 0 if alpha == 0 else min(255, alpha + 18)
    return f"""
QFrame#filePanel {{ background: rgba(255,255,255,{alpha}); border: 1px solid rgba(255,255,255,195); border-radius: 18px; }}
QLabel {{ color: #18202A; background: transparent; }}
QLabel#panelTitle {{ color: #18202A; font-size: 14px; font-weight: bold; }}
QComboBox, QLineEdit {{
    background: rgba(255,255,255,{control_alpha}); color: #18202A;
    border: 1px solid rgba(70,80,95,42); border-radius: 9px; padding: 6px;
    selection-background-color: rgba(55,120,220,150);
}}
QPlainTextEdit, QTextEdit, QListWidget, QTableWidget, QTreeView {{
    background: rgba(255,255,255,{control_alpha}); color: #18202A;
    border: 1px solid rgba(70,80,95,38); border-radius: 10px;
    selection-background-color: rgba(55,120,220,150);
}}
QHeaderView::section {{ background: rgba(235,239,244,230); color: #18202A; border: none; padding: 4px; }}
QComboBox QAbstractItemView {{ background: #F7F9FC; color: #18202A; selection-background-color: #BFD7F7; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: rgba(70,80,95,70); border-radius: 5px; }}
QSplitter#panelSplitter::handle {{ background: rgba(70,80,95,38); border-radius: 3px; }}
QSplitter#panelSplitter::handle:hover {{ background: rgba(55,120,220,150); }}
"""


class FilePanel(QFrame):
    TITLE = t("panel.file")
    RESIZE_MARGIN = 7  # px band at the window edges that triggers drag-resize

    def __init__(self, paths, storage, bus, session, anchor_global=None, parent=None,
                 history=None, show_dialog=True) -> None:
        super().__init__(parent)
        self.paths = [str(Path(p)) for p in paths]
        self.storage = storage
        self.bus = bus
        self.session = session
        self._active_id: Optional[str] = None
        self._resizing = Qt.Edge(0)
        self._move_offset = None  # left-drag on the frame background moves the window

        # Preview-only panels (tiled multi-window compare) must not touch the
        # session references: registering every shown file as a reference made
        # chips pile up and effectively "expanded" dropped folders for good.
        if self.session is not None and show_dialog:
            for p in self.paths:
                self.session.add_reference(p)

        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)  # closing fully tears it down
        self.setObjectName("filePanel")
        self._glass_opacity = glass_opacity()
        self.setStyleSheet(panel_qss(self._glass_opacity))
        glass_theme_changed.opacity_changed.connect(self._apply_glass_opacity)
        self.setMinimumSize(360, 280)
        self.setMouseTracking(True)

        self.models = self.storage.list_model_configs()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        name = " · ".join(Path(p).name for p in self.paths[:3])
        if len(self.paths) > 3:
            name += t("panel.more_items", n=len(self.paths))
        header = QHBoxLayout()
        title = QLabel(f"{self.TITLE}: {name}", self)
        title.setObjectName("panelTitle")
        title.setWordWrap(True)
        header.addWidget(title, 1)
        close_btn = QPushButton("×", self)
        close_btn.setFixedSize(26, 26)
        close_btn.setStyleSheet(
            "QPushButton { color:#25303D; background: rgba(255,255,255,125); border:none; border-radius:13px; font-size:16px; }"
            "QPushButton:hover { background: rgba(235,90,110,235); }"
        )
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn, 0, Qt.AlignTop)
        outer.addLayout(header)

        # A draggable splitter between the preview (display) area and the input
        # area lets the user change their relative sizes (长按拖动分隔条).
        splitter = QSplitter(Qt.Vertical, self)
        splitter.setObjectName("panelSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        self._preview_pane = QWidget(splitter)
        preview_box = QVBoxLayout(self._preview_pane)
        preview_box.setContentsMargins(0, 0, 0, 0)
        preview_box.setSpacing(4)
        try:
            self.build_preview(preview_box)
        except Exception as exc:  # never let a preview failure block the panel
            preview_box.addWidget(QLabel(t("panel.preview_unavailable", err=exc), self))
        splitter.addWidget(self._preview_pane)

        bottom_pane = QWidget(splitter)
        bottom_box = QVBoxLayout(bottom_pane)
        bottom_box.setContentsMargins(0, 0, 0, 0)
        bottom_box.setSpacing(8)
        row = QHBoxLayout()
        self.model_combo = QComboBox(self)
        for r in self.models:
            self.model_combo.addItem(r["display_name"], userData=r["display_name"])
        self.model_combo.setFixedWidth(150)
        try:  # default to the assigned chat-role model, if any
            from pillow_assistant.core.model_roles import load_roles
            idx = self.model_combo.findData(load_roles().get("chat"))
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
        except Exception:
            pass
        row.addWidget(self.model_combo)
        self.prompt_edit = QLineEdit(self)
        self.prompt_edit.setPlaceholderText(t("panel.ask_placeholder"))
        row.addWidget(self.prompt_edit, 1)
        bottom_box.addLayout(row)
        self.response_view = QPlainTextEdit(self)
        self.response_view.setReadOnly(True)
        self.response_view.setMinimumHeight(80)
        bottom_box.addWidget(self.response_view)
        splitter.addWidget(bottom_pane)

        splitter.setStretchFactor(0, 1)  # preview takes the extra space
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([420, 180])
        splitter.splitterMoved.connect(lambda *_: (self._position_grip(), self._on_resized()))
        outer.addWidget(splitter, 1)

        # Preview-only mode (e.g. tiled multi-window compare): the group shares
        # one external dialog bar, so this panel hides its own input/answer pane.
        self._bottom_pane = bottom_pane
        if not show_dialog:
            bottom_pane.hide()

        self.prompt_edit.returnPressed.connect(self._on_submit)
        if self.bus is not None:
            self.bus.event.connect(self._on_event)
        if not self.models:
            self.prompt_edit.setEnabled(False)
            self.response_view.setPlainText(t("panel.no_models"))

        # Show recent conversation history in the answer area so the dialog
        # doesn't start blank.
        if history:
            lines = []
            for turn in history:
                who = t("role.user") if turn.get("role") == "user" else t("role.assistant")
                lines.append(f"【{who}】{turn.get('content', '')}")
            self.response_view.setPlainText("\n".join(lines) + "\n")

        # Ctrl+left-drag anywhere (even over the table/preview content, which
        # consumes plain clicks) moves the window.
        for ch in self.findChildren(QWidget):
            ch.installEventFilter(self)

        # Resize grip lives at the bottom-right of the preview (display) area.
        self._grip = _CornerGrip(self, self._preview_pane)
        self._apply_initial_size()
        self._position_grip()
        if anchor_global is not None:
            self.move(anchor_global)

    def _is_selectable_view(self, obj) -> bool:
        """True if obj (or its parent) is an item view, where Ctrl/Shift-click
        means multi-select — so the window-move gesture must not steal it."""
        from PySide6.QtWidgets import QAbstractItemView

        w = obj
        for _ in range(3):  # obj may be the view's viewport
            if isinstance(w, QAbstractItemView):
                return True
            w = w.parent() if hasattr(w, "parent") else None
            if w is None:
                break
        return False

    def eventFilter(self, obj, event):  # noqa: N802
        et = event.type()
        if et == QEvent.MouseButtonPress and event.button() == Qt.LeftButton \
                and (event.modifiers() & Qt.ControlModifier) and not self._is_selectable_view(obj):
            self._move_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            return True
        if et == QEvent.MouseMove and self._move_offset is not None \
                and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._move_offset)
            return True
        if et == QEvent.MouseButtonRelease and self._move_offset is not None:
            self._move_offset = None
            return True
        return super().eventFilter(obj, event)

    # -- resize handle + content rescale hook -------------------------------
    def _position_grip(self) -> None:
        grip = getattr(self, "_grip", None)
        pane = getattr(self, "_preview_pane", None)
        if grip is not None and pane is not None:
            grip.move(pane.width() - grip.width() - 6, pane.height() - grip.height() - 6)
            grip.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_grip()
        self._on_resized()

    def _apply_glass_opacity(self, opacity: int) -> None:
        self._glass_opacity = max(0, min(100, int(opacity)))
        self.setStyleSheet(panel_qss(self._glass_opacity))
        if self.isVisible():
            if self._glass_opacity == 0:
                disable_acrylic(self)
            else:
                enable_acrylic(self, white_acrylic_color(self._glass_opacity))
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._position_grip()
        self._on_resized()
        self._apply_glass_opacity(glass_opacity())

    def _on_resized(self) -> None:
        """Hook for subclasses to rescale content to the new size."""
        pass

    # -- subclasses override -------------------------------------------------
    def build_preview(self, layout: QVBoxLayout) -> None:
        layout.addWidget(QLabel(t("panel.no_preview"), self))

    def initial_size(self) -> tuple[int, int]:
        """Preferred window size for this file; clamped to the screen. Override
        per type to fit the content."""
        return (560, 540)

    def _apply_initial_size(self) -> None:
        w, h = self.initial_size()
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            w = min(w, int(avail.width() * 0.85))
            h = min(h, int(avail.height() * 0.85))
        self.resize(max(self.minimumWidth(), int(w)), max(self.minimumHeight(), int(h)))

    # -- shared input + streaming -------------------------------------------
    def _question_references(self) -> list:
        """Paths a question from this panel should target. Subclasses can
        narrow this (e.g. the folder panel scopes to a selected file)."""
        if self.session is not None:
            return self.session.references
        return self.paths

    def _on_submit(self) -> None:
        text = self.prompt_edit.text().strip()
        if not text or self.bus is None or self._active_id is not None or not self.models:
            return
        refs = self._question_references()
        request = AppRequest(
            kind=RequestKind.TEXT, prompt=text,
            model_ref=self.model_combo.currentData(), references=refs,
        )
        self._active_id = request.id
        self.response_view.appendPlainText(f"> {text}\n")
        self.prompt_edit.clear()
        self.prompt_edit.setEnabled(False)
        self.bus.submit(request)

    def _on_event(self, event: AgentEvent) -> None:
        if event.request_id != self._active_id:
            return
        if event.type == EventType.TOKEN:
            self.response_view.moveCursor(QTextCursor.MoveOperation.End)
            self.response_view.insertPlainText(event.text)
        elif event.type == EventType.ERROR:
            self.response_view.appendPlainText(f"\n{t('common.error_prefix')} {event.text}")
            self._finish()
        elif event.type == EventType.DONE:
            self.response_view.appendPlainText("\n")
            self._finish()

    def _finish(self) -> None:
        self._active_id = None
        self.prompt_edit.setEnabled(bool(self.models))
        self.prompt_edit.setFocus()

    # -- drag-to-resize on a frameless window ------------------------------
    def _edges_at(self, pos):
        edges = Qt.Edge(0)
        if pos.x() <= self.RESIZE_MARGIN:
            edges |= Qt.LeftEdge
        elif pos.x() >= self.width() - self.RESIZE_MARGIN:
            edges |= Qt.RightEdge
        if pos.y() <= self.RESIZE_MARGIN:
            edges |= Qt.TopEdge
        elif pos.y() >= self.height() - self.RESIZE_MARGIN:
            edges |= Qt.BottomEdge
        return edges

    def _update_resize_cursor(self, edges) -> None:
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges in (Qt.LeftEdge, Qt.RightEdge):
            self.setCursor(Qt.SizeHorCursor)
        elif edges in (Qt.TopEdge, Qt.BottomEdge):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            edges = self._edges_at(event.position().toPoint())
            if edges:
                self._resizing = edges
                self._resize_geo = QRect(self.geometry())
                self._resize_mouse = event.globalPosition().toPoint()
                event.accept()
                return
            # Press on the frame background (header / margins): move the window.
            self._move_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resizing:
            self._do_resize(event.globalPosition().toPoint())
            event.accept()
            return
        if self._move_offset is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._move_offset)
            event.accept()
            return
        self._update_resize_cursor(self._edges_at(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._resizing or self._move_offset is not None:
            self._resizing = Qt.Edge(0)
            self._move_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _do_resize(self, gmouse) -> None:
        g = QRect(self._resize_geo)
        d = gmouse - self._resize_mouse
        minw, minh = self.minimumWidth(), self.minimumHeight()
        edges = self._resizing
        if edges & Qt.LeftEdge:
            nx, nw = g.x() + d.x(), g.width() - d.x()
            if nw < minw:
                nx -= (minw - nw)
                nw = minw
            g.setX(nx)
            g.setWidth(nw)
        elif edges & Qt.RightEdge:
            g.setWidth(max(minw, g.width() + d.x()))
        if edges & Qt.TopEdge:
            ny, nh = g.y() + d.y(), g.height() - d.y()
            if nh < minh:
                ny -= (minh - nh)
                nh = minh
            g.setY(ny)
            g.setHeight(nh)
        elif edges & Qt.BottomEdge:
            g.setHeight(max(minh, g.height() + d.y()))
        self.setGeometry(g)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.bus is not None:
            try:
                self.bus.event.disconnect(self._on_event)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(event)
