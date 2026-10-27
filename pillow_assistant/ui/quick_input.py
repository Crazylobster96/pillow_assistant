"""Summon-on-demand text input bar (FSM left-click / file-drop entry point).

Shows referenced files as removable chips, a compact model picker, a one-line
prompt field, and an inline streamed response area. Submitting builds an
AppRequest (carrying the session's references) and sends it on the event bus.
Files dropped on the bar become references too. While a request runs, a stop
button cancels it; a bar opened while one is running picks up its transcript
from the RunTracker and keeps following it. The bar is frameless,
non-activating, and disappears on Esc / focus-out.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType, RequestKind
from pillow_assistant.core.i18n import t
from pillow_assistant.ui.acrylic import disable_acrylic, glass_opacity, glass_theme_changed
from pillow_assistant.ui.panels.base_panel import _CornerGrip


# No outer frame: the window/panel is transparent. The model picker, the prompt
# box and the answer view are each their own dark, semi-transparent piece —
# translucent enough to read as glass, opaque enough to keep text legible.
def _glass_alpha(opacity: int) -> int:
    """Map 0..100 opacity to exact transparent/opaque alpha endpoints."""
    normalized = max(0, min(100, opacity)) / 100.0
    return round(255 * normalized)


def panel_qss(opacity: int) -> str:
    glass_alpha = _glass_alpha(opacity)
    # Controls add only a very thin layer on top of the glass background.
    control_alpha = 0 if glass_alpha == 0 else max(3, round(glass_alpha * 0.15))
    return f"""
QFrame#quickInput {{ background: transparent; border: none; }}
QLabel {{ color: #18202A; background: transparent; }}
QLabel#toolStatus {{
    color: #FFFFFF; background: rgba(24,34,48,235);
    border: 1px solid rgba(255,255,255,90); border-radius: 9px;
    padding: 8px 10px; font-weight: bold;
}}
QComboBox, QLineEdit {{
    background: rgba(255,255,255,{control_alpha}); color: #18202A;
    border: 1px solid rgba(70,80,95,52); border-radius: 10px; padding: 7px;
    selection-background-color: rgba(55,120,220,150);
}}
QPlainTextEdit {{
    background: rgba(255,255,255,{control_alpha}); color: #18202A;
    border: 1px solid rgba(70,80,95,48); border-radius: 10px; padding: 8px;
    selection-background-color: rgba(55,120,220,150);
}}
QComboBox QAbstractItemView {{ background: rgba(247,249,252,225); color: #18202A; selection-background-color: #BFD7F7; }}
QPushButton {{ color: #25303D; background: rgba(255,255,255,55); border: none; border-radius: 8px; padding: 2px 8px; }}
QPushButton:hover {{ background: rgba(255,255,255,120); }}
"""
log = logging.getLogger(__name__)


class QuickInputBar(QFrame):
    RESIZE_MARGIN = 7  # px band at the window edges that triggers drag-resize

    def __init__(self, storage, bus, session, anchor_global=None, open_reference=None, parent=None,
                 history=None, tracker=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.bus = bus
        self.session = session
        self.open_reference = open_reference  # callable(path) -> reopen its preview
        self.tracker = tracker  # RunTracker: transcripts of runs started from any dialog
        self._active_id: Optional[str] = None
        self._resizing = Qt.Edge(0)

        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        # WA_StyledBackground is required for an objectName-styled QFrame to
        # actually paint its background.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("quickInput")
        self._glass_opacity = glass_opacity()
        self.setStyleSheet(panel_qss(self._glass_opacity))
        glass_theme_changed.opacity_changed.connect(self._apply_glass_opacity)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

        self.models = self.storage.list_model_configs()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(6)
        self.chips_container = QWidget(self)
        self.chips_container.setLayout(self.chips_row)
        outer.addWidget(self.chips_container)

        top = QHBoxLayout()
        self.model_combo = QComboBox(self)
        for row in self.models:
            self.model_combo.addItem(f"{row['display_name']}", userData=row["display_name"])
        self.model_combo.setFixedWidth(160)
        try:  # default to the assigned chat-role model, if any
            from pillow_assistant.core.model_roles import load_roles
            idx = self.model_combo.findData(load_roles().get("chat"))
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
        except Exception:
            pass
        top.addWidget(self.model_combo)

        self.prompt_edit = QLineEdit(self)
        self.prompt_edit.setPlaceholderText(t("input.placeholder"))
        self.prompt_edit.setMinimumWidth(360)
        top.addWidget(self.prompt_edit, 1)
        self.stop_btn = QPushButton(t("input.stop"), self)
        self.stop_btn.setToolTip(t("input.stop_tip"))
        self.stop_btn.setStyleSheet(
            "QPushButton { color:#FFFFFF; background: rgba(215,70,85,225); border:none;"
            " border-radius:9px; padding:4px 10px; }"
            "QPushButton:hover { background: rgba(235,90,110,245); }"
        )
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.hide()
        top.addWidget(self.stop_btn, 0)
        close_btn = QPushButton("×", self)
        close_btn.setFixedSize(26, 26)
        close_btn.setToolTip(t("input.close_tip"))
        close_btn.setStyleSheet(
            "QPushButton { color:#25303D; background: rgba(255,255,255,125); border:none;"
            " border-radius:13px; font-size:16px; }"
            "QPushButton:hover { background: rgba(235,90,110,235); }"
        )
        close_btn.clicked.connect(self.close)
        top.addWidget(close_btn, 0)
        outer.addLayout(top)

        self.tool_status = QLabel(self)
        self.tool_status.setObjectName("toolStatus")
        self.tool_status.setWordWrap(True)
        self.tool_status.hide()
        outer.addWidget(self.tool_status)

        self.response_view = QPlainTextEdit(self)
        self.response_view.setReadOnly(True)
        self.response_view.setMinimumHeight(90)
        self.response_view.hide()
        outer.addWidget(self.response_view, 1)  # expands when the bar is resized

        # Preload recent conversation history so the dialog doesn't start blank.
        if history:
            lines = []
            for turn in history:
                who = t("role.user") if turn.get("role") == "user" else t("role.assistant")
                lines.append(f"【{who}】{turn.get('content', '')}")
            self.response_view.setPlainText("\n".join(lines) + "\n")
            self.response_view.show()

        self.prompt_edit.returnPressed.connect(self._on_submit)
        if self.bus is not None:
            self.bus.event.connect(self._on_event)

        if not self.models:
            self.prompt_edit.setEnabled(False)
            self.prompt_edit.setPlaceholderText(t("input.no_models"))

        # Bottom-right grip to drag-resize the bar; the answer area grows with it.
        self.setMinimumWidth(420)
        self._grip = _CornerGrip(self)
        self._refresh_chips()
        self._adopt_running()
        if anchor_global is not None:
            self.adjustSize()
            self.move(anchor_global)
        self._position_grip()

    def _adopt_running(self) -> None:
        """Show and keep following a run that is still going (its dialog was closed)."""
        run = self.tracker.latest_running() if self.tracker is not None else None
        if run is None or self.bus is None:
            return
        self._active_id = run.request_id
        self._had_tool = False
        self.response_view.show()
        self.response_view.appendPlainText(t("input.resumed") + f"\n> {run.prompt}\n")
        self.response_view.moveCursor(QTextCursor.MoveOperation.End)
        self.response_view.insertPlainText(run.text)
        self._set_tool_status(t("tool.status.processing"))
        if run.last_event is not None:
            self._on_event(run.last_event)
        self.prompt_edit.setEnabled(False)
        self.stop_btn.show()
        if self.height() < 300:
            self.resize(max(self.width(), 520), 340)

    def _position_grip(self) -> None:
        grip = getattr(self, "_grip", None)
        if grip is not None:
            grip.move(self.width() - grip.width() - 5, self.height() - grip.height() - 5)
            grip.raise_()

    def paintEvent(self, event) -> None:  # noqa: N802
        """Paint one true-alpha glass layer instead of an opaque styled frame."""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        alpha = _glass_alpha(self._glass_opacity)
        painter.setBrush(QColor(255, 255, 255, alpha))
        painter.setPen(QPen(QColor(255, 255, 255, 195), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 18, 18)
        painter.end()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_grip()

    def _apply_glass_opacity(self, opacity: int) -> None:
        self._glass_opacity = max(0, min(100, int(opacity)))
        self.setStyleSheet(panel_qss(self._glass_opacity))
        self.update()
        if self.isVisible():
            disable_acrylic(self)
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_glass_opacity(glass_opacity())
    # -- references chips ---------------------------------------------------
    def _refresh_chips(self) -> None:
        while self.chips_row.count():
            item = self.chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        refs = self.session.references if self.session else []
        shown = [p for p in (self.session.displayed if self.session else []) if p not in refs]
        if not refs and not shown:
            self.chips_container.hide()
            return
        self.chips_container.show()
        for path in refs:
            self.chips_row.addWidget(self._make_chip(path))
        for path in shown:  # on screen in a preview window: goes with the question too
            self.chips_row.addWidget(self._make_chip(path, displayed=True))
        self.chips_row.addStretch(1)

    def _make_chip(self, path: str, displayed: bool = False) -> QWidget:
        chip = QFrame(self)
        chip.setAttribute(Qt.WA_StyledBackground, True)
        chip.setStyleSheet(
            "QFrame { background: rgba(70, 110, 170, 235); border-radius: 9px; }"
            "QLabel { color: #F4F8FC; background: transparent; }"
            "QPushButton { color: #F4F8FC; background: transparent; border: none; }"
        )
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(8, 2, 4, 2)
        lay.setSpacing(4)
        name = Path(path).name or path
        # The name is a button: clicking it (re)opens the file's preview panel.
        icon = "👁 " if displayed else ("📁 " if Path(path).is_dir() else "📄 ")
        name_btn = QPushButton(icon + name, chip)
        name_btn.setToolTip(path + "\n" + t("chip.displayed" if displayed else "chip.reopen"))
        name_btn.setCursor(Qt.PointingHandCursor)
        name_btn.setFlat(True)
        name_btn.setStyleSheet("QPushButton { background: transparent; border: none; text-align: left; }")
        name_btn.clicked.connect(lambda: self._open_reference(path))
        lay.addWidget(name_btn)
        remove = QPushButton("×", chip)
        remove.setFixedSize(18, 18)
        remove.clicked.connect(lambda: self._remove_ref(path, displayed))
        lay.addWidget(remove)
        return chip

    def _open_reference(self, path: str) -> None:
        if self.open_reference is not None:
            self.open_reference(path)

    def _remove_ref(self, path: str, displayed: bool = False) -> None:
        if self.session:
            if displayed:
                self.session.remove_displayed(path)
            else:
                self.session.remove_reference(path)
        self._refresh_chips()

    # -- dropping files on the bar adds them as references --------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        if self.session is not None:
            for path in paths:
                self.session.add_reference(path)
        self._refresh_chips()
        self.prompt_edit.setFocus()

    # -- submit / stream ----------------------------------------------------
    def _on_submit(self) -> None:
        text = self.prompt_edit.text().strip()
        if not text or self.bus is None or self._active_id is not None or not self.models:
            return
        refs = self.session.request_references() if self.session else []
        request = AppRequest(
            kind=RequestKind.TEXT,
            prompt=text,
            model_ref=self.model_combo.currentData(),
            references=refs,
            meta={"new_references": self.session.take_new(refs)} if self.session else {},
        )
        self._active_id = request.id
        self._had_tool = False
        self._set_tool_status(t("tool.status.processing"))
        self.response_view.show()
        self.response_view.appendPlainText(f"> {text}\n")
        self.prompt_edit.clear()
        self.prompt_edit.setEnabled(False)
        self.stop_btn.show()
        if self.height() < 300:  # first answer: open to a usable size, but keep any manual resize
            self.resize(max(self.width(), 520), 340)
        self._position_grip()
        if self.tracker is not None:
            self.tracker.start(request.id, text)
        self.bus.submit(request)

    def _on_stop(self) -> None:
        if self._active_id is None or self.bus is None:
            return
        self._set_tool_status(t("tool.status.stopping"))
        self.stop_btn.setEnabled(False)
        self.bus.cancel(self._active_id)

    def _set_tool_status(self, text: str) -> None:
        self.tool_status.setText(text)
        self.tool_status.show()
        self.tool_status.raise_()
    def _on_event(self, event: AgentEvent) -> None:
        if event.request_id != self._active_id:
            return
        try:
            meta = event.meta or {}
            if event.type == EventType.START:
                self._set_tool_status(t("tool.status.processing"))
            elif event.type == EventType.TOOL_START:
                self._had_tool = True
                self._set_tool_status(t(
                    "tool.status.running",
                    step=meta.get("step", 1), total=meta.get("total", "?"),
                    name=meta.get("name") or event.text,
                ))
            elif event.type == EventType.ASK:
                self._set_tool_status(t(
                    "tool.status.waiting",
                    name=meta.get("tool_name") or t("tool.status.permission"),
                ))
            elif event.type == EventType.TOOL_RESULT:
                self._had_tool = True
                result = (event.text or "").strip()
                if len(result) > 500:
                    result = result[:500] + "..."
                key = "tool.status.ok" if meta.get("ok") else "tool.status.failed"
                self._set_tool_status(t(key, name=meta.get("name") or "-", result=result))
            elif event.type == EventType.TOKEN:
                self.response_view.moveCursor(QTextCursor.MoveOperation.End)
                self.response_view.insertPlainText(event.text)
            elif event.type == EventType.ERROR:
                self._set_tool_status(t("tool.status.error", result=event.text))
                self.response_view.appendPlainText(f"\n{t('common.error_prefix')} {event.text}")
                self._finish()
            elif event.type == EventType.DONE:
                if meta.get("cancelled"):
                    self._set_tool_status(t("tool.status.stopped"))
                    self.response_view.appendPlainText(f"\n{t('input.stopped')}")
                elif not getattr(self, "_had_tool", False):
                    self._set_tool_status(t("tool.status.complete"))
                self.response_view.appendPlainText("\n")
                self._finish()
        except Exception:
            log.exception("QuickInputBar._on_event failed (event=%s)", event.type)
    def _finish(self) -> None:
        self._active_id = None
        self.stop_btn.hide()
        self.stop_btn.setEnabled(True)
        self.prompt_edit.setEnabled(bool(self.models))
        self.prompt_edit.setFocus()

    # -- drag-to-resize on a frameless window (any edge / corner) -----------
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
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resizing:
            self._do_resize(event.globalPosition().toPoint())
            event.accept()
            return
        self._update_resize_cursor(self._edges_at(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._resizing:
            self._resizing = Qt.Edge(0)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _do_resize(self, gmouse) -> None:
        g = QRect(self._resize_geo)
        d = gmouse - self._resize_mouse
        minw = max(self.minimumWidth(), 1)
        minh = max(self.minimumHeight(), 1)
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

    # -- dismissal ----------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # Keep open while a response is streaming; otherwise dismiss.
        if self._active_id is None and not self.isActiveWindow():
            self.close()
        super().focusOutEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.bus is not None:
            try:
                self.bus.event.disconnect(self._on_event)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(event)
