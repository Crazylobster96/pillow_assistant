"""Web page preview panel: HTML and SVG shown live (scripts, animation, WebGL)
through Qt WebEngine, so interactive pages and figures play in Pillow.
Falls back to the source text when WebEngine is missing."""

from __future__ import annotations

import webbrowser
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.text_panel import TextPanel

try:  # PySide6-Addons
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    HAVE_WEB = True
except ImportError:  # pragma: no cover
    HAVE_WEB = False


class WebPanel(TextPanel):
    TITLE = t("panel.web")

    def build_preview(self, layout: QVBoxLayout) -> None:
        if not HAVE_WEB:
            layout.addWidget(QLabel(t("panel.web_unavailable"), self))
            super().build_preview(layout)
            return
        path = Path(self.paths[0])
        row = QHBoxLayout()
        row.addStretch(1)
        external = QPushButton(t("panel.web_open_browser"), self)
        external.clicked.connect(lambda: webbrowser.open(path.resolve().as_uri()))
        row.addWidget(external)
        layout.addLayout(row)
        self.web = QWebEngineView(self)
        settings = self.web.settings()
        # Figures load their own scripts, fonts and images next to the page or from a CDN.
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, True)
        # The panel is translucent; an opaque page keeps the figure readable.
        self.web.page().setBackgroundColor(QColor("#FFFFFF"))
        self.web.load(QUrl.fromLocalFile(str(path.resolve())))
        layout.addWidget(self.web, 1)

    def initial_size(self) -> tuple[int, int]:
        return (1000, 760) if HAVE_WEB else super().initial_size()
