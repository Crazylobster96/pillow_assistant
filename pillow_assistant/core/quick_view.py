""""Show me the result": open the project's newest outputs without an agent run.

A request that only asks to look ("看下结果", "打开看看", "再打开结果看一下") needs
no planning: the agent used to check files with a script, start viewers and take
snapshots before showing anything. Here the newest output of each kind (3D model,
drawing, figure, toolpath preview, document, table, image) is found on disk and opened in display windows.
"""

from __future__ import annotations

import re
from pathlib import Path

# Only looking, nothing to change: short, and made of looking words alone.
VIEW_ONLY = re.compile(
    r"^再?(打开|看|看看|看下|看一下|展示|显示|给我看|瞧瞧)(一下|下)?"
    r"(结果|产物|效果|模型|图纸|图|成品)?(看|看看)?(一下|下)?[。.!！?？~～]*$")
SKIP = {"tmp", "checks", "_norton_", "__pycache__", "renders", "shots", "node_modules", "AppData", "~",
        "cadgen-daemon", "kit", "scripts"}
KINDS = (  # first match wins: a CAM preview is an .svg, an anatomy figure an .html
    ("model", (".step", ".stp", ".stl")),
    ("figure", (".html",)),
    ("toolpath", (".preview.svg",)),
    ("drawing", (".pdf",)),
    ("document", (".docx", ".pptx", ".md")),
    ("table", (".xlsx", ".csv")),
    ("image", (".png", ".jpg", ".jpeg", ".svg")),
)
MAX_VIEWS = 3
MAX_SCAN = 8000


def asks_to_view(prompt: str) -> bool:
    return bool(VIEW_ONLY.match(re.sub(r"\s+", "", prompt or "")))


def latest(workspace: Path) -> list[Path]:
    """The newest output of each kind, newest first, at most MAX_VIEWS."""
    best: dict[str, Path] = {}
    try:
        for i, path in enumerate(Path(workspace).rglob("*")):
            if i >= MAX_SCAN:
                break
            rel = path.relative_to(workspace)
            if any(part in SKIP or part.startswith(".") for part in rel.parts[:-1]) or not path.is_file():
                continue
            name = path.name.lower()
            for kind, suffixes in KINDS:
                if name.endswith(suffixes):
                    if kind == "figure" and "figures" not in rel.parts:
                        continue  # an anatomy figure page, not any HTML
                    old = best.get(kind)
                    if old is None or path.stat().st_mtime > old.stat().st_mtime:
                        best[kind] = path
                    break
    except OSError:
        pass
    return sorted(best.values(), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_VIEWS]
