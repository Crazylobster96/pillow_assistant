"""Read-only tool for on-demand retrieval from the active project memory."""

from __future__ import annotations

from pillow_assistant.capabilities.tool_manifest import manifest_tool

from pillow_assistant.capabilities.prompt_registry import render_prompt
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult


@manifest_tool
class RequestProjectMemoryTool:
    name = "request_project_memory"
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        service = getattr(ctx, "project_memory", None)
        project_id = getattr(ctx, "project_id", None)
        if service is None or not project_id:
            return ToolResult(ok=False, text="Project memory is not available for this run.")
        if int(getattr(ctx, "memory_request_count", 0)) >= 2:
            return ToolResult(ok=False, text="The per-turn project-memory retrieval limit (2) was reached.")
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, text="A non-empty project-memory query is required.")
        try:
            top_k = max(1, min(20, int(args.get("top_k") or 8)))
        except (TypeError, ValueError):
            top_k = 8
        ctx.memory_request_count = int(getattr(ctx, "memory_request_count", 0)) + 1
        try:
            result = service.request_memory(
                project_id, query, kinds=args.get("kinds"), task_id=args.get("task_id"),
                required=bool(args.get("required")), top_k=top_k,
                origin_turn_id=getattr(ctx, "request_id", None),
                reason=str(args.get("reason") or ""),
            )
        except Exception as exc:
            return ToolResult(ok=False, text=f"Project-memory retrieval failed: {exc}")
        hits = result.get("hits") or []
        lines = [
            render_prompt("project.retrieval.warning")
        ]
        for hit in hits:
            lines.append(
                f"- source={hit.get('source_id', hit.get('id', '-'))} "
                f"kind={hit.get('kind', '-')} task={hit.get('task_id') or '-'}: "
                f"{hit.get('content', '')}"
            )
        if not hits:
            request = result.get("request")
            if request:
                lines.append(f"No match. A required follow-up request was recorded as {request.get('id')}.")
            else:
                lines.append("No matching project memory was found.")
        return ToolResult(ok=True, text="\n".join(lines))
