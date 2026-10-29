"""The task page beside the question dialog (phase 1: one task at a time).

The dialog shows only the conversation: the user's words and the answer. Once a
request starts using tools it is a task, and its process goes here instead:
status, step and time, the steps and tool output, the independent checks, and
pause / continue / stop. The panel sits against the dialog (right, else left,
top edges aligned, same height) and moves and closes with it; the run itself
keeps going on the bus when the dialog closes.

Status icons: green triangle = running, yellow dot = preparing, red bars = paused,
green square = finished, grey square = stopped or failed.
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPolygon, QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.acrylic import glass_opacity

PANEL_WIDTH = 420
GAP = 8

# state -> (shape, colour); drawn, not typed, so no font can turn them into boxes
STATES = {
    "preparing": ("dot", "#E0A800"),
    "running": ("triangle", "#1E9E4A"),
    "paused": ("bars", "#D64545"),
    "done": ("square", "#1E9E4A"),
    "stopped": ("square", "#8A929C"),
}


class StatusIcon(QWidget):
    """A 14 px status mark: dot, triangle, two bars or a square, in its state's colour."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.shape, self.colour = STATES["preparing"]

    def set_state(self, state: str) -> None:
        self.shape, self.colour = STATES.get(state, STATES["stopped"])
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
        painter.end()


def beside(dialog: QRect, width: int, area: QRect) -> QRect:
    """The panel's place: against the dialog's right edge, else its left, top edges
    aligned, as tall as the dialog; kept on screen."""
    x = dialog.right() + 1 + GAP
    if x + width > area.right() + 1:
        x = dialog.left() - GAP - width
    x = max(area.left(), min(x, area.right() + 1 - width))
    return QRect(x, dialog.top(), width, dialog.height())


class TaskPanel(QWidget):
    def __init__(self, title: str, bus=None, request_id: Optional[str] = None, started: Optional[float] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.bus = bus
        self.request_id = request_id
        self._started = started or time.time()
        self._state = "preparing"
        self._step = ""
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("taskPanel")
        from pillow_assistant.ui.quick_input import panel_qss  # same glass look as the dialog

        self._opacity = glass_opacity()
        self.setStyleSheet(panel_qss(self._opacity))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        head = QHBoxLayout()
        self.icon = StatusIcon(self)
        head.addWidget(self.icon, 0, Qt.AlignTop)
        self.title = QLabel(title, self)
        self.title.setObjectName("taskTitle")
        self.title.setStyleSheet("font-weight: bold;")
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1)
        outer.addLayout(head)
        self.meta = QLabel(self)
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet("color: #4A5563; font-size: 12px;")
        outer.addWidget(self.meta)

        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        outer.addWidget(self.log, 1)

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

    def set_step(self, text: str) -> None:
        self._step = text or ""
        if self._state == "preparing":
            self.set_state("running")
        self._refresh_meta()

    def set_state(self, state: str) -> None:
        self._state = state
        self.icon.set_state(state)
        finished = state in ("done", "stopped")
        self.pause_btn.setVisible(not finished)
        self.stop_btn.setVisible(not finished)
        self.pause_btn.setText(t("task.resume") if state == "paused" else t("task.pause"))
        if finished:
            self._clock.stop()
        self._refresh_meta()

    @property
    def state(self) -> str:
        return self._state

    def _refresh_meta(self) -> None:
        seconds = int(time.time() - self._started)
        parts = [t(f"task.state.{self._state}"), f"{seconds // 60}:{seconds % 60:02d}"]
        if self._step and self._state not in ("done", "stopped"):
            parts.append(self._step)
        self.meta.setText(" · ".join(parts))

    # -- buttons ---------------------------------------------------------------
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
