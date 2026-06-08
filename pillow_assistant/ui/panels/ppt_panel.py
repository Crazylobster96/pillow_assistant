"""PowerPoint preview panel: extracts per-slide text via python-pptx."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel


class PptPanel(FilePanel):
    TITLE = "PPT"

    def build_preview(self, layout: QVBoxLayout) -> None:
        ext = Path(self.paths[0]).suffix.lower()
        if ext == ".ppt":
            layout.addWidget(QLabel(t("panel.ppt_legacy"), self))
            return
        try:
            from pillow_assistant.core.textextract import read_pptx_text

            text = read_pptx_text(self.paths[0])
        except ImportError:
            layout.addWidget(QLabel(t("panel.ppt_need_pkg"), self))
            return
        except Exception as exc:
            layout.addWidget(QLabel(t("panel.ppt_read_failed", err=exc), self))
            return

        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setPlainText(text or t("panel.ppt_empty"))
        layout.addWidget(view)

    def initial_size(self) -> tuple[int, int]:
        return (660, 600)
