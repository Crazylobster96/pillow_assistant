"""run_python tool (migrated from the hardcoded loop tool into the registry)."""

from __future__ import annotations

from pillow_assistant.capabilities.tool_manifest import manifest_tool

import asyncio

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.sandbox import Sandbox

# Seconds a run may take: the model asks per call (``timeout``) within the cap;
# both can be changed in ~/.pillow/settings.json (python_timeout, python_timeout_max).
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600


def time_limits() -> tuple[int, int]:
    from pillow_assistant.core.settings import load_settings

    settings = load_settings()

    def number(key: str, fallback: int) -> int:
        try:
            return max(5, int(settings.get(key) or fallback))
        except (TypeError, ValueError):
            return fallback

    cap = number("python_timeout_max", MAX_TIMEOUT)
    return min(number("python_timeout", DEFAULT_TIMEOUT), cap), cap


def timeout_for(args: dict) -> int:
    default, cap = time_limits()
    try:
        asked = int(args.get("timeout") or default)
    except (TypeError, ValueError):
        asked = default
    return max(5, min(asked, cap))


@manifest_tool
class PythonTool:
    name = "run_python"
    permission = Permission.WRITE_WS

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        code = args.get("code", "")
        timeout = timeout_for(args)
        sandbox = Sandbox(ctx.workspace)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: sandbox.run_python(code, timeout=timeout))
        text = result.summary()
        if getattr(result, "timed_out", False):
            _, cap = time_limits()
            text += t("tool.sandbox_timeout_hint", cap=cap) if timeout < cap else t("tool.sandbox_timeout_cap")
        return ToolResult(ok=(result.returncode == 0), text=text, artifacts=result.artifacts)
