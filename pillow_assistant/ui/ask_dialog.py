"""Ask dialog: shown when the Agent calls ask_user. Presents a question with
optional choices (single click-to-pick, or multi-select checkboxes) and an
"other" free-text field; reports the answer back through a callback. Frameless
dark glass, left-drag to move, Esc cancels."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pillow_assistant.core.i18n import t

QSS = """
QWidget#askRoot {
    background-color: #F7F8FA;
    border: 2px solid #AEB8C4;
    border-radius: 14px;
}
QLabel { color: #17202A; background: transparent; }
QLabel#askTitle { font-size: 14px; font-weight: bold; color: #2767A8; }
QLabel#askQ { font-size: 14px; color: #17202A; }
QLabel#askHint { font-size: 12px; color: #52606D; }
QCheckBox { color: #17202A; background: transparent; padding: 5px 2px; spacing: 8px; }
QCheckBox::indicator {
    width: 17px; height: 17px; border-radius: 4px;
    border: 1px solid #7C8998; background: #FFFFFF;
}
QCheckBox::indicator:checked { background: #3978BC; border: 1px solid #3978BC; }
QLineEdit {
    background: #FFFFFF; color: #17202A;
    border: 1px solid #8D99A8; border-radius: 8px; padding: 8px;
    selection-background-color: #3978BC;
}
QPushButton {
    color: #17202A; background: #FFFFFF;
    border: 1px solid #9BA6B3; border-radius: 8px;
    padding: 8px 12px; text-align: left;
}
QPushButton:hover { background: #E8EEF5; border-color: #6E7C8C; }
QPushButton:focus { border: 2px solid #3978BC; }
QPushButton#askSubmit, QPushButton#askCancel { text-align: center; min-width: 88px; }
QPushButton#askCancel { background: #E9EDF2; }
QPushButton#askSubmit {
    color: #FFFFFF; background: #3978BC;
    border-color: #3978BC; font-weight: bold;
}
QPushButton#askSubmit:hover { background: #2F69A7; }
"""


class AskDialog(QWidget):
    def __init__(self, spec: dict, on_answer: Callable[[dict], None], parent=None) -> None:
        super().__init__(parent)
        self._on_answer = on_answer
        self._answered = False
        self._drag_offset = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowModality(Qt.ApplicationModal)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setObjectName("askRoot")
        self.setStyleSheet(QSS)
        self.setMinimumWidth(440)
        self.setMaximumWidth(560)

        options = [str(o) for o in (spec.get("options") or []) if str(o).strip()]
        self._multi = bool(spec.get("multi"))
        allow_text = bool(spec.get("allow_text", not options))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        outer.addWidget(QLabel(t("ask.title"), self, objectName="askTitle"))
        q = QLabel(spec.get("question", ""), self)
        q.setObjectName("askQ")
        q.setWordWrap(True)
        outer.addWidget(q)
        if self._multi and options:
            outer.addWidget(QLabel(t("ask.multi_hint"), self, objectName="askHint"))

        self._checks: list[QCheckBox] = []
        if self._multi:
            # Multi-select: each option is a checkbox; submit collects them.
            for opt in options:
                cb = QCheckBox(opt, self)
                self._checks.append(cb)
                outer.addWidget(cb)
        else:
            # Single-select: each option picks immediately on click.
            for opt in options:
                btn = QPushButton(opt, self)
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(lambda _=False, o=opt: self._answer(o))
                outer.addWidget(btn)

        self.input = None
        if allow_text:
            self.input = QLineEdit(self)
            self.input.setPlaceholderText(t("ask.other_ph") if options else t("ask.input_ph"))
            self.input.returnPressed.connect(self._submit)
            outer.addWidget(self.input)

        # A submit button is needed whenever there's something to collect:
        # multi-select checkboxes, or a free-text field.
        need_submit = self._multi or allow_text
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(t("ask.cancel"), self, objectName="askCancel")
        cancel.clicked.connect(self._cancel)
        row.addWidget(cancel)
        if need_submit:
            submit = QPushButton(t("ask.submit"), self, objectName="askSubmit")
            submit.clicked.connect(self._submit)
            row.addWidget(submit)
        outer.addLayout(row)

        if self.input is not None:
            self.input.setFocus()

    # -- answering ----------------------------------------------------------
    def _collect(self) -> str:
        """Join checked options + the custom 'other' text into one answer."""
        parts = [cb.text() for cb in self._checks if cb.isChecked()]
        if self.input is not None:
            custom = self.input.text().strip()
            if custom:
                parts.append(custom)
        return t("ask.join_sep").join(parts)

    def _submit(self) -> None:
        answer = self._collect()
        if answer:
            self._answer(answer)

    def _answer(self, text: str) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            self._on_answer({"answer": text, "cancelled": False})
        finally:
            self.close()

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
