"""Project management tools: let the Agent delete a project on the user's
explicit request (by name or id)."""

from __future__ import annotations

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult


class DeleteProjectTool:
    name = "delete_project"
    permission = Permission.SYSTEM
    description = t("tool.delproj.desc")
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": t("tool.delproj.name")}},
        "required": ["name"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        store = getattr(ctx, "project_store", None)
        if store is None:
            return ToolResult(ok=False, text=t("tool.delproj.no_store"))
        query = (args.get("name") or "").strip()
        if not query:
            return ToolResult(ok=False, text=t("tool.delproj.empty"))

        # Resolve by id first, else by name (exact, then substring).
        project = store.get(query)
        if project is None:
            matches = store.find_by_name(query)
            if not matches:
                return ToolResult(ok=False, text=t("tool.delproj.not_found", name=query))
            if len(matches) > 1:
                listing = "\n".join(f"- {p.name} (id={p.id})" for p in matches)
                return ToolResult(ok=False, text=t("tool.delproj.ambiguous", name=query, listing=listing))
            project = matches[0]

        pid, pname = project.id, project.name
        memory_store = getattr(getattr(ctx, "project_memory", None), "store", None)
        if memory_store is not None:
            try:
                memory_store.flush_events(pid)
            except Exception as exc:
                return ToolResult(ok=False, text=f"Project memory could not be prepared for deletion: {exc}")
        if not store.delete(pid):
            return ToolResult(ok=False, text=t("tool.delproj.failed", name=pname))
        if memory_store is not None:
            try:
                memory_store.delete_project_memory(pid)
            except Exception as exc:
                return ToolResult(
                    ok=False,
                    text=f"Project files were deleted, but structured memory cleanup failed: {exc}",
                )

        # If the deleted project is the current conversation's, unbind it so the
        # next turn falls back to one-off chat (no dangling project_id).
        note = ""
        session = getattr(ctx, "session", None)
        if session is not None and getattr(session, "project_id", None) == pid:
            session.project_id = None
            session.session_id = None
            note = t("tool.delproj.current_cleared")
        return ToolResult(ok=True, text=t("tool.delproj.done", name=pname, id=pid) + note)
