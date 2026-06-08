"""Text / code preview panel (read-only, monospace, horizontal scroll)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel

MAX_CHARS = 60_000


class TextPanel(FilePanel):
    TITLE = t("panel.text")

    def build_preview(self, layout: QVBoxLayout) -> None:
        try:
            text = Path(self.paths[0]).read_text("utf-8", errors="replace")
        except OSError as exc:
            text = t("panel.text_read_failed", err=exc)
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + t("panel.truncated")
        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)  # long lines scroll horizontally
        view.setFont(QFont("Consolas", 10))
        view.setPlainText(text)
        layout.addWidget(view)

    def initial_size(self) -> tuple[int, int]:
        return (640, 560)
