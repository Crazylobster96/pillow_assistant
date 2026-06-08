"""Persistent model-role assignments (Agent-configurable).

Roles map a purpose to a model so the Agent (or the user) can pick different
models for different work:

  * ``chat``   -> display_name of the default conversation/agent model
  * ``vision`` -> display_name of the model used when images are involved
  * ``asr``    -> {"backend": "sensevoice"|"whisper", "model": "<whisper size>"}

Stored in ``~/.pillow/model_roles.json``; consulted by the model router, the
ASR module and the UI's default model selection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

ROLES = ("chat", "vision", "asr")


def roles_path() -> Path:
    return Path.home() / ".pillow" / "model_roles.json"


def load_roles(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else roles_path()
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_roles(roles: dict, path: Optional[Path] = None) -> None:
    p = Path(path) if path else roles_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(roles, ensure_ascii=False, indent=2), "utf-8")


def assign(role: str, value: Any, path: Optional[Path] = None) -> dict:
    roles = load_roles(path)
    roles[role] = value
    save_roles(roles, path)
    return roles
