"""excel_inspect / excel_edit: spreadsheet jobs in one deterministic call.

``excel_inspect`` reports a workbook's structure (header row, columns, blocks of
equal values, validations, fonts) without changing anything. ``excel_edit``
applies a list of operations (reorder blocks, replace, set font, renumber)
with openpyxl so formatting survives, writes ``<name>_修改.xlsx`` beside the
source unless told otherwise, and checks the saved file
(``capabilities/excel_ops.py``). Both accept files in the workspace or ones the
user attached; editing writes outside the workspace, so it is SYSTEM permission.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pillow_assistant.capabilities import excel_ops
from pillow_assistant.capabilities.tool_manifest import manifest_tool
from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.builtin.file_tool import _is_reference, _under


def _source(ctx: ToolContext, raw: str) -> Path:
    ws = Path(ctx.workspace)
    path = Path(raw) if Path(raw).is_absolute() else ws / raw
    if not raw or not (_under(ws, path) or _is_reference(ctx, path)):
        raise excel_ops.ExcelError(t("tool.excel.denied", path=raw))
    return path


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


@manifest_tool
class ExcelInspectTool:
    name = "excel_inspect"
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            path = _source(ctx, str(args.get("path") or "").strip())
            info = await asyncio.get_event_loop().run_in_executor(None, lambda: excel_ops.inspect(
                path, sheet=args.get("sheet"), group_by=args.get("group_by"), within=args.get("within")))
        except excel_ops.ExcelError as exc:
            return ToolResult(ok=False, text=str(exc))
        return ToolResult(ok=True, text=_dump(info))


@manifest_tool
class ExcelEditTool:
    name = "excel_edit"
    permission = Permission.SYSTEM

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            path = _source(ctx, str(args.get("path") or "").strip())
            output = str(args.get("output") or "").strip() or None
            if output and not Path(output).is_absolute():
                output = str(path.with_name(output))
            ops = args.get("operations") or []
            if not isinstance(ops, list):
                raise excel_ops.ExcelError("operations must be a list")
            report = await asyncio.get_event_loop().run_in_executor(None, lambda: excel_ops.edit(
                path, ops, sheet=args.get("sheet"), output=output, overwrite=bool(args.get("overwrite"))))
        except excel_ops.ExcelError as exc:
            return ToolResult(ok=False, text=str(exc))
        return ToolResult(ok=True, text=_dump(report), artifacts=[report["output"]])
