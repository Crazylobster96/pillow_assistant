"""The question box: grows with what is typed up to MAX_LINES, then scrolls.

A drop-in for the QLineEdit it replaces (``text`` / ``setText`` / ``clear`` /
``returnPressed``): Enter sends, Shift+Enter starts a new line, and text pasted
or typed beyond five lines scrolls inside the box instead of pushing the dialog
taller.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QFrame, QPlainTextEdit, QSizePolicy

MAX_LINES = 5


class PromptEdit(QPlainTextEdit):
    returnPressed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("promptEdit")
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(lambda *_: self._fit())
        self._fit()

    # -- QLineEdit-compatible surface ----------------------------------------
    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802
        self.setPlainText(text or "")
        self.moveCursor(QTextCursor.MoveOperation.End)

    # -- behaviour -----------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.returnPressed.emit()
            return
        super().keyPressEvent(event)

    def _lines(self) -> int:
        """Visual lines the text takes at the current width (wrapped lines count)."""
        layout = self.document().documentLayout()
        return max(1, int(round(layout.documentSize().height())))

    def _fit(self) -> None:
        line = self.fontMetrics().lineSpacing()
        shown = min(self._lines(), MAX_LINES)
        margins = self.contentsMargins()
        pad = int(self.document().documentMargin() * 2) + margins.top() + margins.bottom()
        self.setFixedHeight(line * shown + pad + 6)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if self._lines() > MAX_LINES
                                        else Qt.ScrollBarAlwaysOff)
