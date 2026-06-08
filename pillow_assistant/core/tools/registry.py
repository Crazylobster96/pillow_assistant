"""Tool registry (T0): holds tools, produces OpenAI tool schemas, dispatches calls."""

from __future__ import annotations

from typing import Any

from pillow_assistant.core.i18n import t as _t
from pillow_assistant.core.tools.base import ToolContext, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in self._tools.values()
        ]

    async def dispatch(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, text=_t("tool.unknown", name=name))
        try:
            return await tool(args, ctx)
        except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
            return ToolResult(ok=False, text=_t("tool.error", name=name, err=exc))
