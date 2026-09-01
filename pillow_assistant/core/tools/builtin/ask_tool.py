"""ask_user tool: pause and ask the user a question when the Agent is unsure.

The Agent can offer multiple-choice options and/or let the user type a free-text
answer. The run blocks until the user responds (or a timeout), then the answer
is fed back to the model so it can continue.
"""

from __future__ import annotations

from pillow_assistant.capabilities.tool_manifest import manifest_tool

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult


@manifest_tool
class AskUserTool:
    name = "ask_user"
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        ask = getattr(ctx, "ask", None)
        if ask is None:
            return ToolResult(ok=False, text=t("tool.ask.no_ui"))
        question = (args.get("question") or "").strip()
        if not question:
            return ToolResult(ok=False, text=t("tool.ask.empty"))
        options = [str(o) for o in (args.get("options") or []) if str(o).strip()]
        multi = bool(args.get("multi_select"))
        allow_text = args.get("allow_text")
        if allow_text is None:
            # Free text by default when no options, or always offered as the
            # "other" field in multi-select.
            allow_text = (not options) or multi

        result = await ask({"question": question, "options": options,
                            "allow_text": bool(allow_text), "multi": multi})
        if result.get("timeout"):
            return ToolResult(ok=False, text=t("tool.ask.timeout"))
        if result.get("cancelled"):
            return ToolResult(ok=False, text=t("tool.ask.cancelled"))
        answer = str(result.get("answer", "")).strip()
        if not answer:
            return ToolResult(ok=False, text=t("tool.ask.cancelled"))
        return ToolResult(ok=True, text=t("tool.ask.answered", answer=answer))
