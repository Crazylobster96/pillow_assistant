"""run_python tool (migrated from the hardcoded loop tool into the registry)."""

from __future__ import annotations

import asyncio

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.sandbox import Sandbox


class PythonTool:
    name = "run_python"
    permission = Permission.WRITE_WS
    description = t("tool.py.desc")
    parameters = {
        "type": "object",
        "properties": {"code": {"type": "string", "description": t("tool.py.code")}},
        "required": ["code"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        code = args.get("code", "")
        sandbox = Sandbox(ctx.workspace)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: sandbox.run_python(code))
        return ToolResult(ok=(result.returncode == 0), text=result.summary(), artifacts=result.artifacts)
