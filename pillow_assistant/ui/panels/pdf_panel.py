"""PDF preview panel: renders the first pages with PyMuPDF if available."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel

MAX_PAGES = 5


def _import_fitz():
    try:
        import fitz  # PyMuPDF's classic module name
        return fitz
    except ImportError:
        try:
            import pymupdf as fitz  # newer alias
            return fitz
        except ImportError:
            return None


class PdfPanel(FilePanel):
    TITLE = "PDF"

    def build_preview(self, layout: QVBoxLayout) -> None:
        fitz = _import_fitz()
        if fitz is None:
            layout.addWidget(QLabel(t("panel.pdf_need_pkg"), self))
            return

        try:
            doc = fitz.open(self.paths[0])
        except Exception as exc:
            layout.addWidget(QLabel(t("panel.pdf_open_failed", err=exc), self))
            return

        if getattr(doc, "needs_pass", False):
            layout.addWidget(QLabel(t("panel.pdf_encrypted"), self))
            doc.close()
            return

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setAlignment(Qt.AlignTop)
        try:
            for i in range(min(MAX_PAGES, doc.page_count)):
                page = doc.load_page(i)
                pix = page.get_pixmap(matrix=fitz.Matrix(1.3, 1.3), alpha=False)
                # copy() detaches the QImage from PyMuPDF's buffer before close.
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
                label = QLabel(container)
                label.setAlignment(Qt.AlignCenter)
                label.setPixmap(QPixmap.fromImage(img).scaledToWidth(480, Qt.SmoothTransformation))
                vbox.addWidget(label)
            if doc.page_count > MAX_PAGES:
                vbox.addWidget(QLabel(t("panel.pdf_pages", total=doc.page_count, shown=MAX_PAGES), container))
        except Exception as exc:
            vbox.addWidget(QLabel(t("panel.pdf_render_failed", err=exc), container))
        finally:
            doc.close()
        scroll.setWidget(container)
        layout.addWidget(scroll)
