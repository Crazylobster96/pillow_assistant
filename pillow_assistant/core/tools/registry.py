"""Tool registry: schemas, mode filtering, permission gates, and dispatch."""

from __future__ import annotations

from typing import Any, Optional

from pillow_assistant.core.i18n import t as _t
from pillow_assistant.core.tools.base import ToolContext, ToolResult
from pillow_assistant.core.tools.permission_policy import authorize


def _tool_modes(tool: Any) -> tuple[str, ...]:
    modes = getattr(tool, "capability_modes", None)
    if not modes:
        return ("chat", "project")
    return tuple(str(mode) for mode in modes)


def _context_mode(ctx: ToolContext) -> str:
    return "project" if getattr(ctx, "project_id", None) else "chat"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        self._tools[tool.name] = tool

    def clone(self) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._tools = dict(self._tools)
        return registry

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def names(self, mode: Optional[str] = None) -> list[str]:
        return [
            name for name, tool in self._tools.items()
            if mode is None or mode in _tool_modes(tool)
        ]

    def schemas(self, mode: Optional[str] = None) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
            }
            for tool in self._tools.values()
            if mode is None or mode in _tool_modes(tool)
        ]

    def snapshot(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "version": str(getattr(tool, "capability_version", "dynamic")),
                "source": str(getattr(tool, "capability_source", type(tool).__module__)),
                "permission": str(getattr(getattr(tool, "permission", None), "value", "system")),
                "modes": list(_tool_modes(tool)),
            }
            for tool in self._tools.values()
            if mode is None or mode in _tool_modes(tool)
        ]

    async def dispatch(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, text=_t("tool.unknown", name=name))
        mode = _context_mode(ctx)
        if mode not in _tool_modes(tool):
            return ToolResult(ok=False, text=f'Tool "{name}" is not available in {mode} mode.')
        try:
            denial = await authorize(tool, args, ctx)
            if denial is not None:
                return denial
            return await tool(args, ctx)
        except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
            return ToolResult(ok=False, text=_t("tool.error", name=name, err=exc))
