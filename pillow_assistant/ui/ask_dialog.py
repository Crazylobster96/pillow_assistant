"""Ask dialog: shown when the Agent calls ask_user. Presents a question with
optional multiple-choice buttons and/or a free-text field; reports the answer
back through a callback. Frameless dark glass, left-drag to move, Esc cancels."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pillow_assistant.core.i18n import t

QSS = """
QWidget#askRoot { background: rgba(18, 22, 28, 244); border: 1px solid rgba(255,255,255,55); border-radius: 14px; }
QLabel { color: #F2F6FA; background: transparent; }
QLabel#askTitle { font-size: 13px; font-weight: bold; color: #9fc0ec; }
QLabel#askQ { font-size: 14px; }
QLineEdit { background: rgba(10,14,20,235); color:#F4F8FC; border:1px solid rgba(255,255,255,55);
    border-radius: 8px; padding: 7px; selection-background-color:#4a82c0; }
QPushButton { color:#FFFFFF; background: rgba(255,255,255,30); border:none; border-radius:8px; padding:7px 12px; text-align:left; }
QPushButton:hover { background: rgba(90,140,200,235); }
QPushButton#askSubmit, QPushButton#askCancel { text-align:center; }
QPushButton#askSubmit { background: rgba(90,140,200,235); }
QPushButton#askSubmit:hover { background: rgba(110,160,220,245); }
"""


class AskDialog(QWidget):
    def __init__(self, spec: dict, on_answer: Callable[[dict], None], parent=None) -> None:
        super().__init__(parent)
        self._on_answer = on_answer
        self._answered = False
        self._drag_offset = None
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setObjectName("askRoot")
        self.setStyleSheet(QSS)
        self.setMinimumWidth(360)
        self.setMaximumWidth(520)

        options = [str(o) for o in (spec.get("options") or []) if str(o).strip()]
        allow_text = bool(spec.get("allow_text", not options))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        outer.addWidget(QLabel(t("ask.title"), self, objectName="askTitle"))
        q = QLabel(spec.get("question", ""), self)
        q.setObjectName("askQ")
        q.setWordWrap(True)
        outer.addWidget(q)

        for opt in options:
            btn = QPushButton(opt, self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, o=opt: self._answer(o))
            outer.addWidget(btn)

        self.input = None
        if allow_text:
            self.input = QLineEdit(self)
            self.input.setPlaceholderText(t("ask.input_ph"))
            self.input.returnPressed.connect(self._submit_text)
            outer.addWidget(self.input)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(t("ask.cancel"), self, objectName="askCancel")
        cancel.clicked.connect(self._cancel)
        row.addWidget(cancel)
        if allow_text:
            submit = QPushButton(t("ask.submit"), self, objectName="askSubmit")
            submit.clicked.connect(self._submit_text)
            row.addWidget(submit)
        outer.addLayout(row)

        if self.input is not None:
            self.input.setFocus()

    # -- answering ----------------------------------------------------------
    def _answer(self, text: str) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            self._on_answer({"answer": text, "cancelled": False})
        finally:
            self.close()

    def _submit_text(self) -> None:
        if self.input is None:
            return
        text = self.input.text().strip()
        if text:
            self._answer(text)

    def _cancel(self) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            self._on_answer({"answer": "", "cancelled": True})
        finally:
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        # If closed by other means (X, parent teardown), report a cancel once.
        if not self._answered:
            self._answered = True
            try:
                self._on_answer({"answer": "", "cancelled": True})
            except Exception:
                pass
        super().closeEvent(event)

    # -- move / dismiss -----------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)
