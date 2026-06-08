"""Word document preview panel: renders formatted HTML (mammoth / python-docx)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QTextBrowser, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel

# Light text + readable tables over the panel's dark background.
DOC_CSS = (
    "body { color: #e8edf2; font-family: 'Microsoft YaHei', sans-serif; line-height: 1.5; }"
    "h1, h2, h3 { color: #ffffff; }"
    "a { color: #7fb0ee; }"
    "table { border-collapse: collapse; }"
    "td, th { border: 1px solid #5a6470; padding: 4px 8px; }"
)


class DocPanel(FilePanel):
    TITLE = t("panel.doc")

    def build_preview(self, layout: QVBoxLayout) -> None:
        ext = Path(self.paths[0]).suffix.lower()
        if ext == ".doc":
            layout.addWidget(QLabel(t("panel.doc_legacy"), self))
            return
        try:
            from pillow_assistant.core.textextract import read_docx_html

            html = read_docx_html(self.paths[0])
        except ImportError:
            layout.addWidget(QLabel(t("panel.doc_need_pkg"), self))
            return
        except Exception as exc:
            layout.addWidget(QLabel(t("panel.doc_read_failed", err=exc), self))
            return

        view = QTextBrowser(self)
        view.setOpenExternalLinks(True)
        view.document().setDefaultStyleSheet(DOC_CSS)
        view.setHtml(html or t("panel.doc_empty"))
        layout.addWidget(view)

    def initial_size(self) -> tuple[int, int]:
        return (700, 640)
