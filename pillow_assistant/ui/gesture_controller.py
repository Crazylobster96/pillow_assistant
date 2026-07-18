"""Input state machine for the floating assistant icon."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal

LONG_PRESS_MS = 400
DRAG_THRESHOLD_PX = 8


class GestureState(str, Enum):
    IDLE = "idle"
    PRESSED = "pressed"
    RECORDING = "recording"
    DRAGGING = "dragging"


class GestureController(QObject):
    clicked = Signal()
    right_clicked = Signal()
    recording_started = Signal()
    recording_finished = Signal()
    drag_started = Signal()
    move_requested = Signal(object)
    drag_finished = Signal()

    def __init__(self, target, anchor, parent=None) -> None:
        super().__init__(parent or anchor)
        self.target = target
        self.anchor = anchor
        self.state = GestureState.IDLE
        self._press_global = QPoint()
        self._drag_offset = QPoint()
        self._long_timer = QTimer(self)
        self._long_timer.setSingleShot(True)
        self._long_timer.timeout.connect(self._begin_recording)
        target.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is not self.target:
            return False
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.RightButton:
                self.right_clicked.emit()
                return True
            if event.button() == Qt.LeftButton:
                self.state = GestureState.PRESSED
                self._press_global = event.globalPosition().toPoint()
                self._drag_offset = self._press_global - self.anchor.frameGeometry().topLeft()
                self._long_timer.start(LONG_PRESS_MS)
                return True

        if event_type == QEvent.Type.MouseMove:
            if not (event.buttons() & Qt.LeftButton):
                return False
            global_pos = event.globalPosition().toPoint()
            if (self.state == GestureState.PRESSED
                    and (global_pos - self._press_global).manhattanLength() > DRAG_THRESHOLD_PX):
                self._long_timer.stop()
                self.state = GestureState.DRAGGING
                self.drag_started.emit()
            if self.state == GestureState.DRAGGING:
                self.move_requested.emit(global_pos - self._drag_offset)
                return True

        if event_type == QEvent.Type.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._long_timer.stop()
            previous = self.state
            self.state = GestureState.IDLE
            if previous == GestureState.PRESSED:
                self.clicked.emit()
            elif previous == GestureState.RECORDING:
                self.recording_finished.emit()
            elif previous == GestureState.DRAGGING:
                self.drag_finished.emit()
            return True
        return False

    def _begin_recording(self) -> None:
        if self.state != GestureState.PRESSED:
            return
        self.state = GestureState.RECORDING
        self.recording_started.emit()
