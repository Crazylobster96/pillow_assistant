"""Generic panel: file info card for unknown types, media, or multi-file drops."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QListWidget, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class GenericPanel(FilePanel):
    TITLE = t("panel.file")

    def build_preview(self, layout: QVBoxLayout) -> None:
        if len(self.paths) != 1:
            layout.addWidget(QLabel(t("panel.generic_refs", n=len(self.paths)), self))
            listing = QListWidget(self)
            listing.addItems([Path(p).name for p in self.paths])
            layout.addWidget(listing)
            return
        p = Path(self.paths[0])
        lines = [t("panel.generic_name", v=p.name), t("panel.generic_path", v=p)]
        try:
            st = p.stat()
            lines.append(t("panel.generic_size", v=_human_size(st.st_size)))
        except OSError:
            pass
        lines.append(t("panel.generic_type", v=p.suffix or t("panel.generic_no_ext")))
        info = QLabel("\n".join(lines), self)
        info.setWordWrap(True)
        info.setTextInteractionFlags(info.textInteractionFlags())
        layout.addWidget(info)
        layout.addStretch(1)
