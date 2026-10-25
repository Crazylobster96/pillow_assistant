"""Suggest the text-to-cad-anatomy edition when the user talks about CAD.

The standard edition ships no CAD / CAM / concept-diagram tools. When a
message mentions them, the answer starts with a one-line pointer to the
edition that does (the Windows offline package tagged ``-text-to-cad-anatomy``).
It shows once per run, and never in that edition itself: the daily sync
merges main into the text-to-cad branch, so this module ships there too and
switches itself off when the CAD tools are present.
"""

from __future__ import annotations

import importlib.util
from typing import Optional

from pillow_assistant.core.cad_intent import mentions_cad

RELEASES_URL = "https://github.com/Crazylobster96/pillow_assistant/releases?q=text-to-cad-anatomy&expanded=true"

_shown = False


def has_cad_edition() -> bool:
    """True in the text-to-cad edition (its CAD capability module is installed)."""
    try:
        return importlib.util.find_spec("pillow_assistant.capabilities.text_to_cad") is not None
    except (ImportError, ValueError):
        return False


def cad_upgrade_hint(prompt: str) -> Optional[str]:
    """The upgrade line for this message, or None (no CAD mention, already shown, or CAD edition)."""
    global _shown
    if _shown or not mentions_cad(prompt) or has_cad_edition():
        return None
    _shown = True
    from pillow_assistant.core.i18n import t

    return t("core.cad_upgrade_hint", url=RELEASES_URL)


def reset() -> None:
    """Forget that the hint was shown (tests)."""
    global _shown
    _shown = False
