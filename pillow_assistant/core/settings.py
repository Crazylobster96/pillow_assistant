"""Tiny persistent app settings (~/.pillow/settings.json).

Currently holds user-adjustable runtime knobs such as ``max_steps`` (the
Agent's tool-loop step budget). Read per request so changes apply immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def settings_path() -> Path:
    return Path.home() / ".pillow" / "settings.json"


def load_settings(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else settings_path()
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict, path: Optional[Path] = None) -> None:
    p = Path(path) if path else settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")


def set_setting(key: str, value: Any, path: Optional[Path] = None) -> dict:
    s = load_settings(path)
    s[key] = value
    save_settings(s, path)
    return s
