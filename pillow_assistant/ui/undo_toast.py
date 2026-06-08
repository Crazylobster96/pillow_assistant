"""5-second undo toast (R3): a small frameless banner with an 撤销 button that
auto-dismisses after the window. Shown when a reversible action just ran."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from pillow_assistant.core.i18n import t


class UndoToast(QWidget):
    def __init__(self, label: str, on_undo: Callable[[], None], timeout_ms: int = 5000, parent=None) -> None:
        super().__init__(parent)
        self._on_undo = on_undo
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("undoToast")
        self.setStyleSheet(
            "QWidget#undoToast { background: rgba(28, 32, 40, 240); border: 1px solid rgba(255,255,255,55);"
            " border-radius: 10px; }"
            "QLabel { color: #F2F6FA; background: transparent; }"
            "QPushButton { color: #FFFFFF; background: rgba(90,140,200,235); border: none;"
            " border-radius: 7px; padding: 4px 12px; }"
            "QPushButton:hover { background: rgba(110,160,220,245); }"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(10)
        lay.addWidget(QLabel(f"{label}", self))
        btn = QPushButton(t("undo.button"), self)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._do_undo)
        lay.addWidget(btn)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.close)
        self._timer.start(timeout_ms)

    def _do_undo(self) -> None:
        self._timer.stop()
        try:
            self._on_undo()
        finally:
            self.close()
