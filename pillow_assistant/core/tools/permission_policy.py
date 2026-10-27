"""Central authorization policy for tools with elevated permissions."""

from __future__ import annotations

import json
from typing import Any, Optional

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult

CONFIRM_PERMISSIONS = {Permission.NETWORK, Permission.SYSTEM}
MAX_DETAIL = 800
SENSITIVE_PARTS = ("api_key", "apikey", "authorization", "credential", "password", "secret", "token")


def _permission_of(tool: Any) -> Permission:
    value = getattr(tool, "permission", Permission.SYSTEM)
    if isinstance(value, Permission):
        return value
    try:
        return Permission(value)
    except (TypeError, ValueError):
        # Unknown or missing permission declarations are elevated by default.
        return Permission.SYSTEM


def redact_sensitive(value: Any, key: str = "") -> Any:
    """Return a copy with values under credential-like keys removed."""
    if any(part in key.lower() for part in SENSITIVE_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _operation_detail(name: str, args: dict) -> str:
    if name == "run_cli":
        detail = str(args.get("command") or "")
    elif name in {"http_request", "browser_read"}:
        method = str(args.get("method") or "GET").upper() if name == "http_request" else "GET"
        detail = f"{method} {args.get('url') or ''}".strip()
    else:
        try:
            detail = json.dumps(redact_sensitive(args), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            detail = str(redact_sensitive(args))
    detail = detail.strip() or "-"
    return detail if len(detail) <= MAX_DETAIL else detail[:MAX_DETAIL] + "…"


async def authorize(tool: Any, args: dict, ctx: ToolContext) -> Optional[ToolResult]:
    """Return a denial result, or ``None`` when the call may proceed."""
    permission = _permission_of(tool)
    if permission not in CONFIRM_PERMISSIONS:
        return None

    name = str(getattr(tool, "name", "unknown"))
    session = getattr(ctx, "session", None)
    if session is not None and getattr(session, "tool_allowed", None) and session.tool_allowed(name):
        return None
    ask = getattr(ctx, "ask", None)
    if ask is None:
        return ToolResult(ok=False, text=t("tool.permission.no_ui", name=name))

    allow_once = t("tool.permission.allow_once")
    allow_conversation = t("tool.permission.allow_conversation")
    deny = t("tool.permission.deny")
    options = [allow_once, deny]
    if session is not None and getattr(session, "allow_tool", None):
        # A long job calls the same few tools many times; one answer covers them.
        options = [allow_once, allow_conversation, deny]
    permission_name = t(
        "tool.permission.network" if permission == Permission.NETWORK else "tool.permission.system"
    )
    result = await ask({
        "question": t(
            "tool.permission.question",
            name=name,
            permission=permission_name,
            detail=_operation_detail(name, args),
        ),
        "options": options,
        "allow_text": False,
        "multi": False,
        "permission": permission.value,
        "tool_name": name,
    })
    if result.get("timeout"):
        return ToolResult(ok=False, text=t("tool.permission.timeout", name=name))
    answer = str(result.get("answer", "")).strip()
    if result.get("cancelled") or answer not in (allow_once, allow_conversation):
        return ToolResult(ok=False, text=t("tool.permission.denied", name=name))
    if answer == allow_conversation and allow_conversation in options:
        session.allow_tool(name)
    return None
