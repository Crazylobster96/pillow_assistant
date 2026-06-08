"""Map dropped paths to the right preview panel (R2).

``classify`` is a pure extension/kind lookup (unit-testable). ``resolve`` picks
the panel class for a set of dropped paths: a single file maps by type, a folder
or a multi-file drop maps to the archive/listing panel.
"""

from __future__ import annotations

from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TABLE_EXT = {".csv", ".tsv", ".xlsx", ".xls"}
DOC_EXT = {".docx", ".doc"}
PPT_EXT = {".pptx", ".ppt"}
PDF_EXT = {".pdf"}
MEDIA_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".mkv"}
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html", ".css", ".xml", ".sh",
    ".bat", ".sql", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".php",
    ".lua", ".r", ".log",
}


def classify(path: str | Path) -> str:
    """Return a kind key: image|pdf|table|code|media|archive|folder|generic."""
    p = Path(path)
    if p.is_dir():
        return "folder"
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in DOC_EXT:
        return "doc"
    if ext in PPT_EXT:
        return "ppt"
    if ext in TABLE_EXT:
        return "table"
    if ext in TEXT_EXT:
        return "code"
    if ext in MEDIA_EXT:
        return "media"
    if ext in ARCHIVE_EXT:
        return "archive"
    return "generic"


def panel_kind(paths: list[str]) -> str:
    """Pure: the panel kind for a drop (no Qt import). 'multi' for >1 path."""
    if len(paths) != 1:
        return "multi"
    return classify(paths[0])


def resolve(paths: list[str]):
    """Return the FilePanel subclass to open for the given dropped paths."""
    # Imported lazily to avoid a circular import (panels import the base which
    # imports nothing from here).
    from pillow_assistant.ui.panels.image_panel import ImagePanel
    from pillow_assistant.ui.panels.pdf_panel import PdfPanel
    from pillow_assistant.ui.panels.table_panel import TablePanel
    from pillow_assistant.ui.panels.text_panel import TextPanel
    from pillow_assistant.ui.panels.doc_panel import DocPanel
    from pillow_assistant.ui.panels.ppt_panel import PptPanel
    from pillow_assistant.ui.panels.archive_panel import ArchivePanel
    from pillow_assistant.ui.panels.generic_panel import GenericPanel

    if len(paths) != 1:
        return GenericPanel  # multiple files -> a listing panel
    kind = classify(paths[0])
    return {
        "image": ImagePanel,
        "pdf": PdfPanel,
        "doc": DocPanel,
        "ppt": PptPanel,
        "table": TablePanel,
        "code": TextPanel,
        "folder": ArchivePanel,
        "archive": ArchivePanel,
        "media": GenericPanel,
        "generic": GenericPanel,
    }.get(kind, GenericPanel)
