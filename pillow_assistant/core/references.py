"""Materialize referenced paths into model context.

R0.5 has no Agent tools or RAG yet (those arrive in R1/R3), so referenced files
are turned into prompt context here, in a bounded way:

* image files -> returned as paths to attach as multimodal image inputs;
* small text-like files -> inlined into the prompt (capped per file and total);
* directories -> shallow listing of their entries;
* anything else / missing -> a short note with the path.

Files are read on demand and never copied or persisted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from pillow_assistant.core.i18n import t

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".html", ".css",
    ".xml", ".sh", ".bat", ".sql", ".java", ".c", ".cpp", ".h", ".go", ".rs",
    ".rb", ".php", ".lua", ".r", ".log",
}

MAX_INLINE_BYTES = 32 * 1024  # per text file
MAX_TOTAL_BYTES = 128 * 1024  # across all inlined text
MAX_DIR_ENTRIES = 50


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXT


def is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXT


def materialize(paths: Iterable[str]) -> tuple[str, list[str]]:
    """Return ``(context_text, image_paths)`` for the given references."""
    parts: list[str] = []
    images: list[str] = []
    budget = MAX_TOTAL_BYTES

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            parts.append(t("refs.missing", path=raw))
            continue

        if path.is_dir():
            try:
                entries = sorted(os.listdir(path))
            except OSError as exc:
                parts.append(t("refs.dir_unreadable", path=raw, err=exc))
                continue
            shown = entries[:MAX_DIR_ENTRIES]
            more = "" if len(entries) <= MAX_DIR_ENTRIES else t("refs.dir_truncated", n=len(entries))
            parts.append(t("refs.dir", path=raw) + "\n".join(shown) + more)
            continue

        if is_image(path):
            images.append(str(path))
            # Also surface the path in text: tools (e.g. present_windows,
            # file_read) need the literal path, which the visual attachment
            # alone doesn't convey to the model.
            parts.append(t("refs.image", path=raw))
            continue

        if path.suffix.lower() == ".docx":
            try:
                from pillow_assistant.core.textextract import read_docx_text

                text = read_docx_text(path)
                budget -= len(text.encode("utf-8"))
                parts.append(t("refs.file", path=raw) + text)
            except Exception:
                parts.append(t("refs.docx_unreadable", path=raw))
            continue

        if path.suffix.lower() == ".pptx":
            try:
                from pillow_assistant.core.textextract import read_pptx_text

                text = read_pptx_text(path)
                budget -= len(text.encode("utf-8"))
                parts.append(t("refs.file", path=raw) + text)
            except Exception:
                parts.append(t("refs.pptx_unreadable", path=raw))
            continue

        size = path.stat().st_size
        if is_text(path) and size <= MAX_INLINE_BYTES and budget > 0:
            try:
                text = path.read_text("utf-8", errors="replace")
            except OSError as exc:
                parts.append(t("refs.unreadable", path=raw, err=exc))
                continue
            if len(text.encode("utf-8")) > budget:
                text = text[:budget]
            budget -= len(text.encode("utf-8"))
            parts.append(t("refs.file", path=raw) + f"```\n{text}\n```")
        else:
            parts.append(t("refs.not_inlined", path=raw, size=size))

    return "\n\n".join(parts), images
