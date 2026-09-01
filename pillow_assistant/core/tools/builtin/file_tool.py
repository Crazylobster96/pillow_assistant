"""File tools (T0): read / write / list, scoped to the project workspace and
the session's referenced paths."""

from __future__ import annotations

from pillow_assistant.capabilities.tool_manifest import manifest_tool

from pathlib import Path

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult

MAX_READ = 60 * 1024


def _under(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_reference(ctx: ToolContext, target: Path) -> bool:
    try:
        t = str(target.resolve())
    except OSError:
        return False
    for r in (ctx.references or []):
        try:
            rp = Path(r).resolve()
        except OSError:
            continue
        if t == str(rp) or _under(rp, target):
            return True
    return False


@manifest_tool
class FileReadTool:
    name = "file_read"
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw = args.get("path", "")
        ws = Path(ctx.workspace)
        p = (ws / raw) if not Path(raw).is_absolute() else Path(raw)
        if not (_under(ws, p) or _is_reference(ctx, p)):
            return ToolResult(ok=False, text=t("tool.fr.denied", path=raw))
        if not p.exists() or not p.is_file():
            return ToolResult(ok=False, text=t("tool.fr.not_found", path=raw))
        try:
            text = p.read_text("utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(ok=False, text=t("tool.fr.failed", err=exc))
        if len(text) > MAX_READ:
            text = text[:MAX_READ] + t("tool.truncated")
        return ToolResult(ok=True, text=text)


@manifest_tool
class FileWriteTool:
    name = "file_write"
    permission = Permission.WRITE_WS

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw = args.get("path", "")
        content = args.get("content", "")
        ws = Path(ctx.workspace)
        p = (ws / raw) if not Path(raw).is_absolute() else Path(raw)
        if not _under(ws, p):
            return ToolResult(ok=False, text=t("tool.fw.outside"))
        existed = p.exists()
        old_bytes = p.read_bytes() if existed and p.is_file() else None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, "utf-8")
            rel = p.resolve().relative_to(ws.resolve())
        except OSError as exc:
            return ToolResult(ok=False, text=t("tool.fw.failed", err=exc))

        # Make the write undoable for 5s: restore old bytes (or delete a new file).
        undo_token = None
        undo_label = ""
        um = getattr(ctx, "undo_manager", None)
        if um is not None:
            def _undo(path=p, prev=old_bytes, was=existed):
                try:
                    if was and prev is not None:
                        path.write_bytes(prev)
                    elif not was and path.exists():
                        path.unlink()
                except OSError:
                    pass
            undo_label = t("tool.fw.undo_overwrite" if existed else "tool.fw.undo_create", rel=rel)
            undo_token = um.register(undo_label, _undo)
        verb = t("tool.fw.overwrote" if existed else "tool.fw.wrote")
        return ToolResult(ok=True, text=t("tool.fw.result", verb=verb, rel=rel, n=len(content)),
                          artifacts=[str(rel)], undo_token=undo_token, undo_label=undo_label)


@manifest_tool
class FileListTool:
    name = "file_list"
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw = args.get("path", ".") or "."
        ws = Path(ctx.workspace)
        p = (ws / raw) if not Path(raw).is_absolute() else Path(raw)
        if not (_under(ws, p) or _is_reference(ctx, p)):
            return ToolResult(ok=False, text=t("tool.fl.denied", path=raw))
        if not p.is_dir():
            return ToolResult(ok=False, text=t("tool.fl.not_dir", path=raw))
        try:
            entries = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
        except OSError as exc:
            return ToolResult(ok=False, text=t("tool.fl.failed", err=exc))
        return ToolResult(ok=True, text="\n".join(entries[:200]) or t("tool.fl.empty"))
