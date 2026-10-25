"""text-to-cam tools: turn CAD output into machine programs.

``text_to_cam`` (``cut2d``) reads a DXF from the workspace and writes laser /
plasma G-code plus a preview SVG next to it (``capabilities/cam2d.py``).  It runs
in-process with the standard library and only writes inside the workspace.

``cam_mill`` turns a STEP part into 3-axis CNC milling programs through FreeCAD's
CAM workbench (``capabilities/cam_mill.py``), installing FreeCAD on first use.
With ``job.setups`` it writes one program per 3+2 / 4th-axis indexed setup
(``capabilities/cam_indexed.py``).
It runs an external program, so like cad_app it is SYSTEM permission.

Neither connects to a machine.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pillow_assistant.capabilities import cam2d
from pillow_assistant.capabilities.tool_manifest import manifest_tool
from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.builtin.file_tool import _is_reference, _under

MAX_DXF = 50 * 1024 * 1024


def _resolve(ctx: ToolContext, raw: str) -> Path:
    ws = Path(ctx.workspace)
    return (ws / raw) if not Path(raw).is_absolute() else Path(raw)


def _settings(ctx: ToolContext, args: dict, key: str) -> dict:
    """The inline object, layered over the JSON file ``<key>_file`` in the workspace when given."""
    inline = args.get(key) or {}
    if not isinstance(inline, dict):
        raise cam2d.CamError(t("tool.cam.bad_settings", path=key, err="not an object"))
    value = str(args.get(key + "_file") or "").strip()
    if not value:
        return inline
    path = _resolve(ctx, value)
    if not (_under(Path(ctx.workspace), path) or _is_reference(ctx, path)) or not path.is_file():
        raise cam2d.CamError(t("tool.cam.no_settings", key=key, path=value))
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise cam2d.CamError(t("tool.cam.bad_settings", path=value, err=exc)) from None
    if not isinstance(data, dict):
        raise cam2d.CamError(t("tool.cam.bad_settings", path=value, err="not an object"))
    return {**data, **inline}


@manifest_tool
class TextToCamTool:
    name = "text_to_cam"
    permission = Permission.WRITE_WS

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = str(args.get("action") or "cut2d").strip().lower()
        if action != "cut2d":
            return ToolResult(ok=False, text=t("tool.cam.bad_action"))
        ws = Path(ctx.workspace)
        raw = str(args.get("input") or "").strip()
        src = _resolve(ctx, raw)
        if not raw or not (_under(ws, src) or _is_reference(ctx, src)):
            return ToolResult(ok=False, text=t("tool.fr.denied", path=raw))
        if not src.is_file():
            return ToolResult(ok=False, text=t("tool.fr.not_found", path=raw))
        if src.suffix.lower() != ".dxf":
            return ToolResult(ok=False, text=t("tool.cam.not_dxf"))
        if src.stat().st_size > MAX_DXF:
            return ToolResult(ok=False, text=t("tool.cam.too_big"))
        out_raw = str(args.get("output") or "").strip()
        out = _resolve(ctx, out_raw) if out_raw else ws / (src.stem + ".nc")
        if not _under(ws, out):
            return ToolResult(ok=False, text=t("tool.fw.outside"))
        preview = out.with_suffix(".preview.svg")
        try:
            machine = _settings(ctx, args, "machine")
            job = _settings(ctx, args, "job")
            program, svg, st, warnings = cam2d.cut2d(src.read_text("utf-8", errors="replace"), machine, job,
                                                     source=src.name)
        except cam2d.CamError as exc:
            return ToolResult(ok=False, text=t("tool.cam.failed", err=exc))
        except OSError as exc:
            return ToolResult(ok=False, text=t("tool.fr.failed", err=exc))
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(program, "utf-8", newline="\n")
            preview.write_text(svg, "utf-8")
        except OSError as exc:
            return ToolResult(ok=False, text=t("tool.fw.failed", err=exc))
        files = [out.resolve().relative_to(ws.resolve()).as_posix(),
                 preview.resolve().relative_to(ws.resolve()).as_posix()]
        lines = [t("tool.cam.done", gcode=files[0], preview=files[1], cut=f"{st['cut_mm']:.0f}",
                   pierces=st["pierces"], outers=st["outers"], holes=st["holes"], minutes=f"{st['minutes']:.1f}")]
        if warnings:
            lines.append(t("tool.cam.warnings") + "\n" + "\n".join(f"- {w}" for w in warnings))
        lines.append(t("tool.cam.review"))
        return ToolResult(ok=True, text="\n".join(lines), artifacts=files)


@manifest_tool
class CamMillTool:
    name = "cam_mill"
    permission = Permission.SYSTEM

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        from pillow_assistant.capabilities import cad_apps, cam_mill

        if not getattr(ctx, "allow_cli", True):
            return ToolResult(ok=False, text=t("tool.cli.disabled"))
        ws = Path(ctx.workspace)
        raw = str(args.get("input") or "").strip()
        src = _resolve(ctx, raw)
        if not raw or not (_under(ws, src) or _is_reference(ctx, src)):
            return ToolResult(ok=False, text=t("tool.fr.denied", path=raw))
        if not src.is_file():
            return ToolResult(ok=False, text=t("tool.fr.not_found", path=raw))
        if src.suffix.lower() not in (".step", ".stp"):
            return ToolResult(ok=False, text=t("tool.cam.not_step"))
        out_raw = str(args.get("output") or "").strip()
        out = _resolve(ctx, out_raw) if out_raw else ws / (src.stem + ".nc")
        if not _under(ws, out):
            return ToolResult(ok=False, text=t("tool.fw.outside"))
        try:
            machine = _settings(ctx, args, "machine")
            job = _settings(ctx, args, "job")
            if job.get("setups"):  # fail fast, before any download
                from pillow_assistant.capabilities import cam_indexed
                cam_indexed.plan_setups(machine, job)
            else:
                cam_mill.build_spec(machine, job)
        except cam2d.CamError as exc:
            return ToolResult(ok=False, text=t("tool.cam.failed", err=exc))

        loop = asyncio.get_event_loop()
        spec = cad_apps.APPS["freecad"]
        app = cad_apps.locate("freecad")
        if app is None:
            try:
                result = await loop.run_in_executor(None, cad_apps.install, "freecad")
            except Exception as exc:  # noqa: BLE001 - offline, rate limit, unsupported platform
                return ToolResult(ok=False, text=t("tool.cam.no_freecad", err=exc, page=spec.download_page))
            if "opened" in result:
                return ToolResult(ok=False, text=t("tool.cadapp.manual", app=spec.title, page=result["opened"]))
            app = Path(result["path"])
        try:
            result = await loop.run_in_executor(
                None, lambda: cam_mill.mill(src, out, machine, job, cad_apps.command_for(app)))
        except cam2d.CamError as exc:
            return ToolResult(ok=False, text=t("tool.cam.failed", err=exc))
        root = ws.resolve()

        def rel(p: str) -> str:
            path = Path(p).resolve()
            return path.relative_to(root).as_posix() if _under(root, path) else str(path)

        previews = result.get("previews") or [result["preview"]]
        projects = result.get("projects") or ([result["project"]] if result["project"] else [])
        files = ([rel(p) for p in result["programs"]] + [rel(p) for p in previews] + [rel(result["sheet"])]
                 + [rel(p) for p in projects])
        lines = [t("tool.cam.milled", programs=", ".join(rel(p) for p in result["programs"]),
                   preview=", ".join(rel(p) for p in previews), sheet=rel(result["sheet"]),
                   project=", ".join(rel(p) for p in projects) or "-",
                   ops=", ".join(result["operations"]), minutes=f"{result['minutes']:.0f}")]
        if result["warnings"]:
            lines.append(t("tool.cam.warnings") + "\n" + "\n".join(f"- {w}" for w in result["warnings"]))
        lines.append(t("tool.cam.mill_review"))
        return ToolResult(ok=True, text="\n".join(lines), artifacts=files)
