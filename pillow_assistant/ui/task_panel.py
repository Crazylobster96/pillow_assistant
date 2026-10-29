"""The task panel beside the question dialog: one page per task, tabs down its right side.

The dialog shows only the conversation: the user's words and the answers. Once a
request starts using tools it is a task, and its process goes to its own page
here: status, step and time, the steps and tool output, the independent checks,
pause / continue / stop, and a "tell it" box that reaches a running task before its
next step. Several tasks run at once (``core/run_control.max_tasks``); each has a
tab with its status icon. At most MAX_TABS tabs show, ordered running > preparing >
stopped > finished (newest first within each); the rest sit under "more", and
"all" lists every task. The panel sits against the dialog (right, else left, top
edges aligned, same height) and moves and closes with it; the runs keep going on
the bus when the dialog closes.

Status icons: green triangle = running, yellow dot = preparing, red bars = paused,
green square = finished, grey square = stopped or failed.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPolygon, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.acrylic import glass_opacity

PANEL_WIDTH = 560
TAB_WIDTH = 132
GAP = 8
MAX_TABS = 5

# state -> (shape, colour); drawn, not typed, so no font can turn them into boxes
STATES = {
    "preparing": ("dot", "#E0A800"),
    "running": ("triangle", "#1E9E4A"),
    "paused": ("bars", "#D64545"),
    "done": ("square", "#1E9E4A"),
    "stopped": ("square", "#8A929C"),
}
# tab order: what still needs watching first
ORDER = {"running": 0, "paused": 0, "preparing": 1, "stopped": 2, "done": 3}


class StatusIcon(QWidget):
    """A 14 px status mark: dot, triangle, two bars or a square, in its state's colour."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.shape, self.colour = STATES["preparing"]
        self.waiting = False

    def set_state(self, state: str) -> None:
        self.shape, self.colour = STATES.get(state, STATES["stopped"])
        self.update()

    def set_waiting(self, waiting: bool) -> None:
        self.waiting = waiting
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.colour))
        if self.shape == "dot":
            painter.drawEllipse(3, 3, 10, 10)
        elif self.shape == "triangle":
            painter.drawPolygon(QPolygon([QPoint(3, 2), QPoint(14, 8), QPoint(3, 14)]))
        elif self.shape == "bars":
            painter.drawRect(3, 2, 3, 12)
            painter.drawRect(10, 2, 3, 12)
        else:
            painter.drawRect(3, 3, 10, 10)
        if self.waiting:  # an orange "!" over the state: the task waits for the user
            painter.setBrush(QColor("#F08C00"))
            painter.drawEllipse(7, 0, 9, 9)
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawRect(11, 2, 1, 4)
            painter.drawRect(11, 7, 1, 1)
        painter.end()


def beside(dialog: QRect, width: int, area: QRect) -> QRect:
    """The panel's place: against the dialog's right edge, else its left, top edges
    aligned, as tall as the dialog; kept on screen."""
    x = dialog.right() + 1 + GAP
    if x + width > area.right() + 1:
        x = dialog.left() - GAP - width
    x = max(area.left(), min(x, area.right() + 1 - width))
    return QRect(x, dialog.top(), width, dialog.height())


def tab_order(pages: list) -> list:
    """Pages as their tabs line up: waiting for the user, then still-running work, newest first."""
    return sorted(pages, key=lambda p: (-1 if p.waiting else ORDER.get(p.state, 9), -p.started))


class TaskPage(QWidget):
    """One task: header, status, log, pause / continue / stop, and a box to tell it more."""

    def __init__(self, title: str, bus=None, request_id: Optional[str] = None, started: Optional[float] = None,
                 project: str = "", on_followup: Optional[Callable[[str], None]] = None,
                 on_change: Optional[Callable[[], None]] = None, on_switch: Optional[Callable[[str], None]] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.bus = bus
        self.request_id = request_id
        self.title_text = title
        self.project = project
        self.project_id = ""
        self.other_project = False  # not the dialog's current project
        self.waiting = False
        self.seen = False  # looked at after it ended: may be archived
        self._on_switch = on_switch
        self.started = started or time.time()
        self._state = "preparing"
        self._step = ""
        self._on_followup = on_followup  # a finished task: what is said goes as a new request
        self._on_change = on_change  # the panel re-orders its tabs

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        head = QHBoxLayout()
        self.icon = StatusIcon(self)
        head.addWidget(self.icon, 0, Qt.AlignTop)
        self.title = QLabel(title, self)
        self.title.setStyleSheet("font-weight: bold;")
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1)
        self.switch_btn = QPushButton(t("task.switch_project"), self)
        self.switch_btn.clicked.connect(lambda: self._on_switch and self._on_switch(self.project_id))
        self.switch_btn.hide()
        head.addWidget(self.switch_btn, 0, Qt.AlignTop)
        outer.addLayout(head)
        self.meta = QLabel(self)
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet("color: #4A5563; font-size: 12px;")
        outer.addWidget(self.meta)

        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        outer.addWidget(self.log, 1)

        self.say = QLineEdit(self)
        self.say.setPlaceholderText(t("task.say"))
        self.say.returnPressed.connect(self._tell)
        outer.addWidget(self.say)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.pause_btn = QPushButton(t("task.pause"), self)
        self.pause_btn.clicked.connect(self._toggle_pause)
        buttons.addWidget(self.pause_btn)
        self.stop_btn = QPushButton(t("task.stop"), self)
        self.stop_btn.clicked.connect(self._stop)
        buttons.addWidget(self.stop_btn)
        outer.addLayout(buttons)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._refresh_meta)
        self._clock.start(1000)
        self.set_state("preparing")

    # -- what the run reports ------------------------------------------------
    def append(self, text: str) -> None:
        if not text:
            return
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def set_step(self, text: str, queued: bool = False) -> None:
        self._step = text or ""
        if self._state == "preparing" and not queued:
            self.set_state("running")
        self._refresh_meta()

    def set_project(self, name: str, project_id: str, other: bool) -> None:
        self.project, self.project_id, self.other_project = name, project_id, other
        self.switch_btn.setVisible(bool(other and project_id and self._on_switch))
        self._refresh_meta()
        if self._on_change is not None:
            self._on_change()

    def set_waiting(self, waiting: bool) -> None:
        if waiting == self.waiting:
            return
        self.waiting = waiting
        self.icon.set_waiting(waiting)
        self._refresh_meta()
        if self._on_change is not None:
            self._on_change()

    def set_state(self, state: str) -> None:
        self._state = state
        if state in ("done", "stopped"):
            self.waiting = False
            self.icon.set_waiting(False)
        self.icon.set_state(state)
        finished = state in ("done", "stopped")
        self.pause_btn.setVisible(not finished)
        self.stop_btn.setVisible(not finished)
        self.pause_btn.setText(t("task.resume") if state == "paused" else t("task.pause"))
        if finished:
            self._clock.stop()
        self._refresh_meta()
        if self._on_change is not None:
            self._on_change()

    @property
    def state(self) -> str:
        return self._state

    def _refresh_meta(self) -> None:
        seconds = int(time.time() - self.started)
        parts = [t("task.waiting") if self.waiting else t(f"task.state.{self._state}"),
                 f"{seconds // 60}:{seconds % 60:02d}"]
        if self.project:
            parts.insert(0, self.project + (" ↗" if self.other_project else ""))
        if self._step and self._state not in ("done", "stopped"):
            parts.append(self._step)
        self.meta.setText(" · ".join(parts))

    # -- what the user does ----------------------------------------------------
    def _tell(self) -> None:
        text = self.say.text().strip()
        if not text:
            return
        self.say.clear()
        self.append(f"\n【{t('role.user')}】{text}\n")
        if self._state in ("done", "stopped"):
            if self._on_followup is not None:
                self._on_followup(text)  # a new round: the intent check decides where it goes
        elif self.bus is not None and self.request_id:
            self.bus.tell(self.request_id, text)

    def _toggle_pause(self) -> None:
        if self.bus is None or not self.request_id:
            return
        if self._state == "paused":
            self.bus.resume(self.request_id)
            self.set_state("running")
        else:
            self.bus.pause(self.request_id)
            self.set_state("paused")

    def _stop(self) -> None:
        if self.bus is None or not self.request_id:
            return
        if self._state == "paused":
            self.bus.resume(self.request_id)  # a paused task must reach the cancellation point
        self.stop_btn.setEnabled(False)
        self.bus.cancel(self.request_id)


class _Tab(QPushButton):
    """A tab down the panel's right side: status icon + short name, project underneath."""

    def __init__(self, page: TaskPage, parent=None) -> None:
        super().__init__(parent)
        self.page = page
        self.setCheckable(True)
        self.setFixedWidth(TAB_WIDTH - 12)
        self.setMinimumHeight(36)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 6, 4)
        lay.setSpacing(0)
        lay.addStretch(1)
        # The icon sits on the name's line, centred on it; the project goes underneath.
        row = QHBoxLayout()
        row.setSpacing(6)
        self.icon = StatusIcon(self)
        row.addWidget(self.icon, 0, Qt.AlignVCenter)
        self.name = QLabel(self)
        self.name.setStyleSheet("font-size: 12px; background: transparent;")
        self.name.setText(self.name.fontMetrics().elidedText(page.title_text, Qt.ElideRight, TAB_WIDTH - 48))
        row.addWidget(self.name, 1, Qt.AlignVCenter)
        lay.addLayout(row)
        self.sub = QLabel(self)
        self.sub.setStyleSheet("font-size: 11px; color: #6B7684; background: transparent; padding-left: 22px;")
        mark = " ↗" if page.other_project else ""  # another project's task: the mark always shows
        room = TAB_WIDTH - 54 - self.sub.fontMetrics().horizontalAdvance(mark)
        self.sub.setText(self.sub.fontMetrics().elidedText(page.project, Qt.ElideRight, room) + mark)
        self.sub.setVisible(bool(page.project))
        lay.addWidget(self.sub)
        lay.addStretch(1)
        self.setToolTip(page.title_text)
        self.refresh()

    def refresh(self) -> None:
        self.icon.set_state(self.page.state)
        self.icon.set_waiting(self.page.waiting)


class TaskPanel(QWidget):
    """The window: the selected task's page on the left, the tabs on the right.

    It follows the bus itself, so a panel dragged out as its own window keeps
    updating after the dialog closes. Docked, it sits against the dialog and closes
    with it; dragged anywhere it becomes a separate window (double-click docks it)."""

    detached: Optional["TaskPanel"] = None  # the panel living on as its own window
    ARCHIVE_AFTER_MS = 10 * 60 * 1000  # a finished task the user has seen leaves the tabs

    def __init__(self, bus=None, on_followup: Optional[Callable[[str], None]] = None,
                 current_project: Optional[Callable[[], Optional[str]]] = None,
                 on_switch: Optional[Callable[[str], None]] = None, on_dock: Optional[Callable[[], None]] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.bus = bus
        self._on_followup = on_followup
        self._current_project = current_project or (lambda: None)
        self._on_switch = on_switch
        self._on_dock = on_dock
        self.docked = True
        self._drag_from: Optional[QPoint] = None
        self.pages: dict[str, TaskPage] = {}
        self.current: Optional[str] = None
        self.setToolTip(t("task.detach_tip"))
        if bus is not None and hasattr(bus, "event"):
            bus.event.connect(self.on_event)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("taskPanel")
        from pillow_assistant.ui.quick_input import panel_qss  # same glass look as the dialog

        self._opacity = glass_opacity()
        self.setStyleSheet(panel_qss(self._opacity) + "QPushButton:checked { background: rgba(74,124,200,60); }")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        self.stack = QStackedWidget(self)
        outer.addWidget(self.stack, 1)
        column = QVBoxLayout()
        column.setSpacing(6)
        self.tabs_box = QVBoxLayout()
        self.tabs_box.setSpacing(6)
        column.addLayout(self.tabs_box)
        self.more_btn = QPushButton(self)
        self.more_btn.clicked.connect(self._show_more)
        column.addWidget(self.more_btn)
        column.addStretch(1)
        self.all_btn = QPushButton(t("task.all"), self)
        self.all_btn.clicked.connect(lambda: self._menu(list(self.pages.values()), self.all_btn))
        column.addWidget(self.all_btn)
        holder = QWidget(self)
        holder.setFixedWidth(TAB_WIDTH)
        holder.setLayout(column)
        outer.addWidget(holder, 0)
        self._tabs: list[_Tab] = []
        self._hidden: list[TaskPage] = []
        self._relayout()

    # -- pages -------------------------------------------------------------------
    def add(self, request_id: str, title: str, project: str = "", started: Optional[float] = None) -> TaskPage:
        page = self.pages.get(request_id)
        if page is None:
            page = TaskPage(title, bus=self.bus, request_id=request_id, started=started, project=project,
                            on_followup=lambda text: self._on_followup and self._on_followup(text),
                            on_change=self._page_changed,
                            on_switch=lambda pid: self._on_switch and self._on_switch(pid), parent=self)
            self.pages[request_id] = page
            self.stack.addWidget(page)
        self.select(request_id)
        return page

    def page(self, request_id: Optional[str]) -> Optional[TaskPage]:
        return self.pages.get(request_id) if request_id else None

    def select(self, request_id: str) -> None:
        page = self.pages.get(request_id)
        if page is None:
            return
        self.current = request_id
        self.stack.setCurrentWidget(page)
        self._page_changed()

    def remove(self, request_id: str) -> None:
        page = self.pages.pop(request_id, None)
        if page is None:
            return
        self.stack.removeWidget(page)
        page.deleteLater()
        if self.current == request_id:
            self.current = next(iter(tab_order(list(self.pages.values()))), None)
            if self.current:
                self.stack.setCurrentWidget(self.pages[self.current])
        self._relayout()

    def _page_changed(self) -> None:
        """Re-order the tabs; a finished task once looked at is archived a while later."""
        current = self.pages.get(self.current)
        if current is not None and current.state in ("done", "stopped") and not current.seen:
            current.seen = True
            QTimer.singleShot(self.ARCHIVE_AFTER_MS, lambda rid=current.request_id: self._archive(rid))
        self._relayout()

    def _archive(self, request_id: str) -> None:
        if request_id == self.current and self.isVisible():
            QTimer.singleShot(self.ARCHIVE_AFTER_MS, lambda: self._archive(request_id))  # still being read
            return
        self.remove(request_id)

    def bind(self, on_followup=None, current_project=None, on_switch=None, on_dock=None) -> None:
        """A new dialog takes over a panel that outlived its own (dragged out as a window)."""
        self._on_followup, self._on_switch, self._on_dock = on_followup, on_switch, on_dock
        if current_project is not None:
            self._current_project = current_project

    # -- what the runs report ----------------------------------------------------
    def on_event(self, event) -> None:
        from pillow_assistant.contracts import EventType
        from pillow_assistant.ui.run_tracker import is_dialog_text

        try:
            page = self.pages.get(event.request_id)
        except RuntimeError:
            return
        if page is None:
            return
        meta = event.meta or {}
        if event.type == EventType.TOOL_START:
            if not meta.get("status"):
                page.set_waiting(False)
                page.set_step(t("tool.status.running", step=meta.get("step", 1), total=meta.get("total", "?"),
                                name=meta.get("name") or event.text))
            else:
                page.set_step(meta["status"], queued=bool(meta.get("queued")))
        elif event.type == EventType.ASK:
            page.set_waiting(True)
        elif event.type == EventType.TOOL_RESULT:
            page.set_waiting(False)
        elif event.type == EventType.TOKEN:
            if meta.get("project") is not None:
                pid = meta.get("project_id") or ""
                page.set_project(meta["project"], pid, bool(pid) and pid != self._current_project())
            if not is_dialog_text(event):
                page.append(event.text)
        elif event.type == EventType.ERROR:
            page.set_state("stopped")
        elif event.type == EventType.DONE:
            page.set_state("stopped" if meta.get("cancelled") else "done")

    # -- tabs --------------------------------------------------------------------
    def _relayout(self) -> None:
        if not hasattr(self, "tabs_box"):
            return
        ordered = tab_order(list(self.pages.values()))
        shown = ordered[:MAX_TABS]
        current = self.pages.get(self.current)
        if current is not None and current not in shown and shown:
            shown[-1] = current  # the page being looked at keeps its tab
        self._hidden = [p for p in ordered if p not in shown]
        for tab in self._tabs:
            tab.setParent(None)
            tab.deleteLater()
        self._tabs = []
        for page in shown:
            tab = _Tab(page, self)
            tab.setChecked(page.request_id == self.current)
            tab.clicked.connect(lambda _=False, rid=page.request_id: self.select(rid))
            self.tabs_box.addWidget(tab)
            self._tabs.append(tab)
        self.more_btn.setText(t("task.more", n=len(self._hidden)))
        self.more_btn.setVisible(bool(self._hidden))
        self.all_btn.setVisible(len(self.pages) > 1)

    def _show_more(self) -> None:
        self._menu(self._hidden, self.more_btn)

    def _menu(self, pages: list, under: QWidget) -> None:
        menu = QMenu(self)
        for page in tab_order(pages):
            action = menu.addAction(f"{t(f'task.state.{page.state}')} · {page.title_text[:30]}")
            action.triggered.connect(lambda _=False, rid=page.request_id: self.select(rid))
        menu.exec(under.mapToGlobal(QPoint(0, under.height())))

    # -- docking ---------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            self.docked = False  # moved by hand: a window of its own from now on
            TaskPanel.detached = self
            self.move(event.globalPosition().toPoint() - self._drag_from)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_from = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.docked = True
        if TaskPanel.detached is self:
            TaskPanel.detached = None
        if self._on_dock is not None:
            self._on_dock()
        super().mouseDoubleClickEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if TaskPanel.detached is self:
            TaskPanel.detached = None
        if self.bus is not None and hasattr(self.bus, "event"):
            try:
                self.bus.event.disconnect(self.on_event)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(event)

    # -- placement and look ----------------------------------------------------
    def place_beside(self, dialog: QWidget) -> None:
        screen = dialog.screen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        self.setGeometry(beside(dialog.frameGeometry(), PANEL_WIDTH, area))

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        from pillow_assistant.ui.quick_input import _glass_alpha

        painter.setBrush(QColor(255, 255, 255, _glass_alpha(self._opacity)))
        painter.setPen(QPen(QColor(255, 255, 255, 195), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 18, 18)
        painter.end()


class Toast(QLabel):
    """A short notice near the button that goes away by itself; a click acts on it."""

    def __init__(self, text: str, on_click: Optional[Callable[[], None]] = None, ms: int = 8000) -> None:
        super().__init__(text)
        self._on_click = on_click
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setStyleSheet("QLabel { color: #FFFFFF; background: rgba(24,34,48,235); border-radius: 10px;"
                           " padding: 10px 14px; font-weight: bold; }")
        self.setWordWrap(True)
        self.setMaximumWidth(360)
        QTimer.singleShot(ms, self.close)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._on_click is not None:
            self._on_click()
        self.close()
