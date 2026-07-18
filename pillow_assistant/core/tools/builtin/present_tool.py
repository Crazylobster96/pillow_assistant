"""present_windows tool: open several display windows at once, tiled.

Lets the Agent honor requests like "把这两段结果并排展示" / "左右对比这两个文件":
it emits a SURFACE event (kind="multi") that the UI shell turns into multiple
draggable windows arranged side-by-side (row) or stacked (column). Each item is
either a piece of text/markdown or a file path (shown with its type-adaptive
preview panel).
"""

from __future__ import annotations

from pathlib import Path

from pillow_assistant.contracts import AgentEvent, EventType, SurfaceLevel, SurfaceSpec
from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult

MAX_WINDOWS = 4


class PresentTool:
    name = "present_windows"
    description = t("tool.pw.desc")
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": t("tool.pw.items"),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": t("tool.pw.title")},
                        "text": {"type": "string", "description": t("tool.pw.text")},
                        "path": {"type": "string", "description": t("tool.pw.path")},
                    },
                },
            },
            "layout": {
                "type": "string",
                "enum": ["row", "column"],
                "description": t("tool.pw.layout"),
            },
        },
        "required": ["items"],
    }
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        items = args.get("items") or []
        layout = args.get("layout") or "row"
        if layout not in ("row", "column"):
            layout = "row"
        if not items:
            return ToolResult(ok=False, text=t("tool.pw.no_items"))
        if ctx.emit is None:
            return ToolResult(ok=False, text=t("tool.pw.no_ui"))

        views: list[dict] = []
        skipped: list[str] = []
        for it in items[:MAX_WINDOWS]:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "")
            path = it.get("path")
            text = it.get("text")
            # Model sometimes puts a bare file path in `text` — treat it as a path
            # so the file still opens in its type-adaptive viewer.
            if not path and text:
                path_candidate = str(text).strip().strip('"')
                if (len(path_candidate) < 500 and "\n" not in path_candidate
                        and Path(path_candidate).exists()):  # file OR folder
                    path, text = path_candidate, None
            if path:
                p = Path(str(path))
                if not p.exists():
                    skipped.append(t("tool.pw.missing", path=path))
                    continue
                views.append({"title": title or p.name, "path": str(p)})
            elif text:
                views.append({"title": title or t("multi.content_title"), "text": str(text)})
        if not views:
            return ToolResult(ok=False, text=t("tool.pw.nothing",
                                               detail="；".join(skipped or [t("tool.pw.empty_items")])))

        await ctx.emit(AgentEvent(
            request_id=ctx.request_id or "",
            type=EventType.SURFACE,
            surface=SurfaceSpec(
                level=SurfaceLevel.L5, kind="multi", body="",
                payload={"views": views, "layout": layout},
            ),
        ))
        note = t("tool.pw.done_row" if layout == "row" else "tool.pw.done_column", n=len(views))
        if skipped:
            note += t("tool.pw.skipped") + "；".join(skipped)
        return ToolResult(ok=True, text=note)
