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
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".mpg", ".mpeg"}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT
WEB_EXT = {".html", ".htm", ".svg"}  # shown live: scripts, animation, WebGL
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"}
MODEL_EXT = {".stl", ".step", ".stp"}  # turned in a 3D window
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html", ".css", ".xml", ".sh",
    ".bat", ".sql", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".php",
    ".lua", ".r", ".log",
} - WEB_EXT


def classify(path: str | Path) -> str:
    """Return a kind key: image|pdf|table|web|code|media|archive|model|folder|generic."""
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
    if ext in WEB_EXT:
        return "web"
    if ext in TEXT_EXT:
        return "code"
    if ext in VIDEO_EXT:
        return "video"
    if ext in MEDIA_EXT:
        return "media"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in MODEL_EXT:
        return "model"
    return "generic"


def panel_kind(paths: list[str]) -> str:
    """Pure: the panel kind for a drop (no Qt import). 'multi' for >1 path."""
    if len(paths) != 1:
        return "multi"
    return classify(paths[0])


SIDE_BY_SIDE_MAX = 3
LISTED = {"folder", "archive", "media", "generic"}  # kinds with no viewer of their own


def side_by_side(paths: list[str]) -> bool:
    """Pure: whether a drop of several files opens each in its own viewer, in a row.

    Two STEPs, two images, a PDF and a table: each has a viewer, so they are shown
    beside each other instead of as a listing. Folders, archives and files with no
    viewer of their own, or more than SIDE_BY_SIDE_MAX files, keep the listing panel."""
    return 1 < len(paths) <= SIDE_BY_SIDE_MAX and all(classify(p) not in LISTED for p in paths)


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
    from pillow_assistant.ui.panels.video_panel import VideoPanel
    from pillow_assistant.ui.panels.web_panel import WebPanel
    from pillow_assistant.ui.panels.model_panel import ModelPanel

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
        "web": WebPanel,
        "folder": ArchivePanel,
        "archive": ArchivePanel,
        "video": VideoPanel,
        "model": ModelPanel,
        "media": GenericPanel,
        "generic": GenericPanel,
    }.get(kind, GenericPanel)
