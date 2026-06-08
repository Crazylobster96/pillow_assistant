"""Multi-model routing (T3): pick a model config for a request.

Respects the user's chosen model, but auto-switches to a multimodal (vlm) model
when the request involves images and the chosen one isn't vlm.
"""

from __future__ import annotations

from typing import Any, Optional


def _type_of(configs: list, ref: Optional[str]) -> str:
    for c in configs:
        if c["display_name"] == ref:
            return (c["model_type"] or "").lower()
    return ""


def select_model(configs: list, default_ref: Optional[str], want_vision: bool = False,
                 roles: Optional[dict] = None) -> Optional[str]:
    """Return the display_name of the model to use.

    Priority: vision需求 → vision 角色 / 已选 vlm / 任一 vlm；
    其余 → 用户已选 → chat 角色 → 第一个。
    """
    roles = roles or {}
    names = [c["display_name"] for c in configs]
    if want_vision:
        vr = roles.get("vision")
        if vr in names and _type_of(configs, vr) == "vlm":
            return vr
        if default_ref and _type_of(configs, default_ref) == "vlm":
            return default_ref
        for c in configs:
            if (c["model_type"] or "").lower() == "vlm":
                return c["display_name"]
    if default_ref and default_ref in names:
        return default_ref
    cr = roles.get("chat")
    if cr in names:
        return cr
    return names[0] if names else default_ref
