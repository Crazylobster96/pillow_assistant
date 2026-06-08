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
QWidget#askRoot { background: rgba(18, 22, 28, 244); border: 1px solid rgba(255,255,255,55); border-radius: 14px; }
QLabel { color: #F2F6FA; background: transparent; }
QLabel#askTitle { font-size: 13px; font-weight: bold; color: #9fc0ec; }
QLabel#askQ { font-size: 14px; }
QLabel#askHint { font-size: 12px; color: #8A97A6; }
QCheckBox { color: #EAF0F6; background: transparent; padding: 4px 2px; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid rgba(255,255,255,80); background: rgba(10,14,20,200); }
QCheckBox::indicator:checked { background: #4a82c0; border: 1px solid #4a82c0; }
QLineEdit { background: rgba(10,14,20,235); color:#F4F8FC; border:1px solid rgba(255,255,255,55);
    border-radius: 8px; padding: 7px; selection-background-color:#4a82c0; }
QPushButton { color:#FFFFFF; background: rgba(255,255,255,30); border:none; border-radius:8px; padding:7px 12px; text-align:left; }
QPushButton:hover { background: rgba(90,140,200,235); }
QPushButton#askSubmit, QPushButton#askCancel { text-align:center; }
QPushButton#askSubmit { background: rgba(90,140,200,235); font-weight:bold; }
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
        self.setMinimumWidth(380)
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
        self._