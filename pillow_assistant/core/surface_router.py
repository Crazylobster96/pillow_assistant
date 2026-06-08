"""Surface router (R3, pragmatic subset L1 / L4 / L5).

Picks how a final answer is shown, by its type/size:
  * L5 main window — has artifacts, is long, or looks like code (roomy view).
  * L1 micro-hint  — a very short acknowledgement (just flash the icon).
  * L4 card        — everything else (the default response area).
L0 / L2 / L3 are reserved for later.
"""

from __future__ import annotations

from pillow_assistant.contracts import SurfaceLevel

LONG_CHARS = 800
CODE_HINTS = ("```", "def ", "class ", "import ", "#include", "function ", "<html")


def route(text: str, artifacts: list | None = None, ok: bool = True) -> SurfaceLevel:
    text = text or ""
    artifacts = artifacts or []
    if artifacts:
        return SurfaceLevel.L5
    if len(text) >= LONG_CHARS:
        return SurfaceLevel.L5
    if len(text) > 200 and any(h in text for h in CODE_HINTS):
        return SurfaceLevel.L5
    if len(text.strip()) <= 40:
        return SurfaceLevel.L1
    return SurfaceLevel.L4
