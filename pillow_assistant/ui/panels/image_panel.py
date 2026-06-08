"""Image preview panel — fits the window and rescales with it (keep aspect)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel

# Cap the kept-in-memory pixmap; it is then fit-scaled down to the window.
MAX_KEEP = 2600


class ImagePanel(FilePanel):
    TITLE = t("panel.image")

    def build_preview(self, layout: QVBoxLayout) -> None:
        self._orig = QPixmap(self.paths[0])
        if self._orig.isNull():
            self._img_label = None
            layout.addWidget(QLabel(t("panel.image_load_failed"), self))
            return
        if self._orig.width() > MAX_KEEP or self._orig.height() > MAX_KEEP:
            self._orig = self._orig.scaled(MAX_KEEP, MAX_KEEP, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._img_label = QLabel(self)
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setMinimumSize(1, 1)
        self._img_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._img_label)

    def _on_resized(self) -> None:
        orig = getattr(self, "_orig", None)
        label = getattr(self, "_img_label", None)
        if orig is None or label is None or orig.isNull():
            return
        size = label.size()
        if size.width() > 4 and size.height() > 4:
            label.setPixmap(orig.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def initial_size(self) -> tuple[int, int]:
        o = getattr(self, "_orig", None)
        if o is not None and not o.isNull():
            return (max(420, min(o.width() + 56, 1000)), max(360, min(o.height() + 200, 820)))
        return (560, 480)
