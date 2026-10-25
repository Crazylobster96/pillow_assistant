"""3-axis milling CAM: a STEP part to CNC G-code through FreeCAD's CAM workbench.

Pillow validates the user's machine, stock and tool settings here, writes a
spec, and runs ``cam_freecad_runner.py`` inside ``freecadcmd``.  FreeCAD builds
the job (facing, adaptive roughing and finishing, drilling, outline profile,
3D surface) and posts it with its own post-processor for the controller.  Back
here, the toolpath is checked against the stock and the machine, and a preview
SVG and a setup sheet are written next to the program.

Like ``cam2d``, nothing here guesses spindle speeds, feeds or depths of cut:
they come from the user, and missing ones are an error.  Nothing connects to a
machine.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from pillow_assistant.capabilities.cam2d import CamError, _n

RUNNER = Path(__file__).with_name("cam_freecad_runner.py")
RUN_TIMEOUT = 1800

# Controller name -> FreeCAD post-processor.
POSTS = {"grbl": "grbl", "linuxcnc": "linuxcnc", "fanuc": "fanuc", "mach3": "mach3_mach4",
         "mach4": "mach3_mach4", "centroid": "centroid", "uccnc": "uccnc", "heidenhain": "heidenhain"}
TOOL_SHAPES = {"endmill", "ballend", "bullnose", "drill", "chamfer", "v-bit"}
OPERATIONS = {"face", "rough", "finish", "profile", "drill", "surface", "waterline"}
MILLING_SHAPES = {"endmill", "bullnose"}


def _num(d: dict, key: str, where: str, *, required: bool = False, minimum: Optional[float] = None,
         positive: bool = False) -> Optional[float]:
    if d.get(key) in (None, ""):
        if required:
            raise CamError(f"{where}: {key} is required")
        return None
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        raise CamError(f"{where}: {key} must be a number") from None
    if not math.isfinite(v) or (positive and v <= 0) or (minimum is not None and v < minimum):
        raise CamError(f"{where}: {key} must be {'> 0' if positive else f'>= {minimum:g}'}")
    return v


def build_spec(machine: dict, job: dict) -> dict:
    """Validated FreeCAD job spec (without file paths); raises CamError with what is missing."""
    machine, job = machine or {}, job or {}
    missing = []
    if not machine.get("controller"):
        missing.append("machine.controller")
    if not isinstance(job.get("stock"), dict):
        missing.append("job.stock {xy, top, bottom}")
    if not job.get("tools"):
        missing.append("job.tools")
    if missing:
        raise CamError("missing settings: " + ", ".join(missing) +
                       " (ask the user; never guess speeds, feeds or depths of cut)")
    controller = str(machine["controller"]).lower()
    if controller not in POSTS:
        raise CamError(f"controller must be one of {', '.join(POSTS)}")
    max_rpm = _num(machine, "max_rpm", "machine", positive=True)
    max_feed = _num(machine, "max_feed", "machine", positive=True)
    rapid = _num(machine, "rapid", "machine", positive=True)
    travel = machine.get("travel")
    if travel is not None:
        try:
            travel = [float(travel[0]), float(travel[1]), float(travel[2])]
        except (TypeError, ValueError, IndexError):
            raise CamError("machine.travel must be [x, y, z] in mm") from None

    st = job["stock"]
    stock = {k: _num(st, k, "job.stock", required=True, minimum=0) for k in ("xy", "top", "bottom")}
    tools: list[dict] = []
    seen = set()
    for i, t in enumerate(job["tools"], 1):
        if not isinstance(t, dict):
            raise CamError(f"job.tools[{i}] must be an object")
        where = f"tool {t.get('number', i)}"
        number = int(_num(t, "number", where, required=True, positive=True))
        if number in seen:
            raise CamError(f"tool number {number} is used twice")
        seen.add(number)
        shape = str(t.get("type") or "").lower()
        if shape not in TOOL_SHAPES:
            raise CamError(f"{where}: type must be one of {', '.join(sorted(TOOL_SHAPES))}")
        tool = {"number": number, "shape": shape, "name": str(t.get("name") or f"{_n(float(t.get('diameter') or 0))}mm {shape}")[:60]}
        for key in ("diameter", "rpm", "feed", "plunge"):
            tool[key] = _num(t, key, where, required=True, positive=True)
        if shape != "drill":
            tool["stepdown"] = _num(t, "stepdown", where, required=True, positive=True)
        for key in ("length", "flute_length", "tip_angle", "peck"):
            v = _num(t, key, where, positive=True)
            if v is not None:
                tool[key] = v
        if t.get("flutes") is not None:
            tool["flutes"] = int(_num(t, "flutes", where, positive=True))
        if max_rpm and tool["rpm"] > max_rpm:
            raise CamError(f"{where}: rpm {_n(tool['rpm'])} is above the machine's max_rpm {_n(max_rpm)}")
        for key in ("feed", "plunge"):
            if max_feed and tool[key] > max_feed:
                raise CamError(f"{where}: {key} {_n(tool[key])} is above the machine's max_feed {_n(max_feed)}")
        tools.append(tool)
    by_number = {t["number"]: t for t in tools}

    allowance = _num(job, "finish_allowance", "job", minimum=0)
    allowance = 0.3 if allowance is None else allowance
    ops = job.get("operations") or default_operations(tools, stock, allowance)
    if not ops:
        raise CamError("no operations: add an endmill or a drill to job.tools")
    checked = []
    for i, o in enumerate(ops, 1):
        if not isinstance(o, dict):
            raise CamError(f"operation {i} must be an object")
        kind = str(o.get("type") or "").lower()
        if kind not in OPERATIONS:
            raise CamError(f"operation {i}: type must be one of {', '.join(sorted(OPERATIONS))}")
        try:
            tool = by_number[int(o.get("tool"))]
        except (TypeError, ValueError, KeyError):
            raise CamError(f"operation {i} ({kind}): tool must be one of the job's tool numbers") from None
        if (kind == "drill") != (tool["shape"] == "drill"):
            raise CamError(f"operation {i} ({kind}) cannot use T{tool['number']} ({tool['shape']})")
        if kind in ("surface", "waterline") and tool["shape"] not in ("ballend", "bullnose", "endmill"):
            raise CamError(f"operation {i} ({kind}) needs a ball, bull-nose or flat end mill")
        op = {"type": kind, "tool": tool["number"]}
        defaults = {"face": 60, "rough": 20, "finish": 20, "surface": 10, "waterline": 10, "profile": 50}
        if kind != "drill":
            op["stepover"] = _num(o, "stepover", f"operation {i}", positive=True) or defaults[kind]
            if op["stepover"] > 100:
                raise CamError(f"operation {i}: stepover is a percentage of the tool diameter (<= 100)")
            op["stepdown"] = _num(o, "stepdown", f"operation {i}", positive=True) or tool["stepdown"]
        else:
            op["diameter"] = tool["diameter"]
            if o.get("peck") is not None or tool.get("peck"):
                op["peck"] = _num(o, "peck", f"operation {i}", positive=True) or tool["peck"]
        if kind == "rough":
            op["stock_to_leave"] = _num(o, "stock_to_leave", f"operation {i}", minimum=0)
            if op["stock_to_leave"] is None:
                op["stock_to_leave"] = allowance
        if kind == "profile":
            op["climb"] = bool(o.get("climb", True))
        checked.append(op)

    skin = _num(job, "skin", "job", minimum=0)
    return {
        "post": POSTS[controller], "controller": controller,
        # GRBL has no tool changer: one file per tool, each starting after a manual change.
        "split_by_tool": controller == "grbl" or str(machine.get("tool_change") or "").lower() == "manual",
        "post_args": "--no-show-editor" if controller == "grbl" else "",
        "stock": stock, "skin": 0.3 if skin is None else skin,
        "safe_height": _num(job, "safe_height", "job", positive=True) or 3.0,
        "clearance_height": _num(job, "clearance_height", "job", positive=True) or 5.0,
        "rapid": rapid, "tools": tools, "operations": checked,
        "machine": {"travel": travel, "max_rpm": max_rpm, "max_feed": max_feed, "name": str(machine.get("name") or "")},
        "allow_below_stock": bool(job.get("allow_below_stock", False)),
    }


def default_operations(tools: list[dict], stock: dict, allowance: float) -> list[dict]:
    """A standard 3-axis plan: face, rough, drill, finish, then 3D surface with a ball end mill."""
    mills = sorted((t for t in tools if t["shape"] in MILLING_SHAPES), key=lambda t: -t["diameter"])
    ops: list[dict] = []
    if mills and stock["top"] > 0:
        ops.append({"type": "face", "tool": mills[0]["number"]})
    if mills:
        ops.append({"type": "rough", "tool": mills[0]["number"], "stock_to_leave": allowance})
    ops += [{"type": "drill", "tool": t["number"]} for t in tools if t["shape"] == "drill"]
    if mills:
        ops.append({"type": "finish", "tool": mills[-1]["number"]})
    ops += [{"type": "surface", "tool": t["number"]} for t in tools if t["shape"] == "ballend"]
    return ops


def run_freecad(spec: dict, freecad: list[str], *, runner: Optional[Callable] = None,
                timeout: int = RUN_TIMEOUT) -> dict:
    """Run the FreeCAD side; returns its report (raises CamError when it failed)."""
    with tempfile.TemporaryDirectory(prefix="pillow-cam-") as tmp:
        spec = {**spec, "report": str(Path(tmp) / "report.json")}
        spec_path = Path(tmp) / "spec.json"
        spec_path.write_text(json.dumps(spec), "utf-8")
        env = {**os.environ, "PILLOW_CAM_SPEC": str(spec_path)}
        runner = runner or subprocess.run
        try:
            p = runner(freecad + [str(RUNNER)], cwd=tmp, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CamError(f"FreeCAD did not finish within {timeout} s") from None
        except OSError as exc:
            raise CamError(f"cannot run FreeCAD: {exc}") from None
        try:
            report = json.loads(Path(spec["report"]).read_text("utf-8"))
        except (OSError, ValueError):
            tail = ((p.stderr or "") + (p.stdout or ""))[-1500:]
            raise CamError(f"FreeCAD exited with code {p.returncode} and no report:\n{tail}") from None
    if not report.get("ok"):
        raise CamError("FreeCAD could not build the job: " + str(report.get("error")) +
                       ("\n" + report["traceback"][-1200:] if report.get("traceback") else ""))
    return report


def check(spec: dict, report: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) from the posted toolpath against the stock, tools and machine."""
    errors: list[str] = []
    warnings: list[str] = list(report.get("warnings") or [])
    sx0, sy0, sz0, sx1, sy1, sz1 = report["stock"]
    tools = {t["number"]: t for t in spec["tools"]}
    feed_z = [m[3] for o in report["operations"] for m in o["moves"] if m[0] == 1]
    if not feed_z:
        errors.append("the job produced no cutting moves")
        return errors, warnings
    low = min(feed_z)
    if low < sz0 - 0.01:
        msg = (f"the tool goes to Z{_n(low)}, {_n(sz0 - low)} mm below the bottom of the stock "
               "(into the fixture or spoilboard)")
        (warnings if spec.get("allow_below_stock") else errors).append(msg)
    travel = (spec.get("machine") or {}).get("travel")
    if travel:
        span = (sx1 - sx0, sy1 - sy0, sz1 + spec["clearance_height"] - low)
        for axis, need, have in zip("XYZ", span, travel):
            if need > have + 1e-6:
                errors.append(f"the job needs {_n(need)} mm of {axis} travel; the machine has {_n(have)} mm")
    sideways = 0
    for o in report["operations"]:
        prev = None
        for m in o["moves"]:
            if prev is not None and m[0] == 0 and (abs(m[1] - prev[1]) > 0.01 or abs(m[2] - prev[2]) > 0.01) \
                    and max(m[3], prev[3]) < sz1 - 0.01:
                sideways += 1
            prev = m
        tool = tools[o["tool"]]
        zs = [m[3] for m in o["moves"] if m[0] == 1]
        reach = tool.get("flute_length") or tool.get("length")
        if zs and reach and sz1 - min(zs) > reach + 0.01:
            warnings.append(f"{o['label']}: T{tool['number']} cuts {_n(sz1 - min(zs))} mm deep but its "
                            f"{'flutes are' if tool.get('flute_length') else 'length is'} {_n(reach)} mm")
    if sideways:
        warnings.append(f"{sideways} rapid move(s) travel sideways below the stock top; FreeCAD keeps the tool "
                        "down inside cleared areas, but check them in the simulation")
    return errors, warnings


def _seconds(text: str) -> float:
    try:
        h, m, s = (float(x) for x in str(text).split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return 0.0


def preview_svg(report: dict) -> str:
    """Top (XY) and front (XZ) views of the cutting moves over the stock."""
    sx0, sy0, sz0, sx1, sy1, sz1 = report["stock"]
    w, h, d = sx1 - sx0, sy1 - sy0, sz1 - sz0
    pad = max(w, h) * 0.05 + 2
    gap = pad
    total_h = h + gap + d + 4 * pad
    sw = max(w, total_h) / 700
    palette = ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#17becf"]
    ox, oy_top = -sx0 + pad, pad + sy1  # top view: y up
    oy_front = pad + h + gap + pad + sz1  # front view: z up
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_n(w + 2 * pad)} {_n(total_h)}" '
           f'width="{_n(min(1200, (w + 2 * pad) * 6))}" style="background:#fff">',
           f'<rect x="{_n(pad)}" y="{_n(pad)}" width="{_n(w)}" height="{_n(h)}" fill="#f2f2f2" stroke="#888" '
           f'stroke-width="{_n(sw)}"/>',
           f'<rect x="{_n(pad)}" y="{_n(oy_front - sz1)}" width="{_n(w)}" height="{_n(d)}" fill="#f2f2f2" '
           f'stroke="#888" stroke-width="{_n(sw)}"/>']
    mx0, my0, mz0, mx1, my1, mz1 = report["model"]
    out.append(f'<rect x="{_n(ox + mx0)}" y="{_n(oy_top - my1)}" width="{_n(mx1 - mx0)}" height="{_n(my1 - my0)}" '
               f'fill="none" stroke="#444" stroke-dasharray="{_n(sw * 4)}" stroke-width="{_n(sw)}"/>')
    legend_y = total_h - pad
    for i, o in enumerate(report["operations"]):
        color = palette[i % len(palette)]
        top_pts, front_pts = [], []
        for m in o["moves"]:
            if m[0] != 1:
                if len(top_pts) > 1:
                    out.append(_poly(top_pts, color, sw))
                    out.append(_poly(front_pts, color, sw))
                top_pts, front_pts = [], []
                continue
            top_pts.append((ox + m[1], oy_top - m[2]))
            front_pts.append((ox + m[1], oy_front - m[3]))
        if len(top_pts) > 1:
            out.append(_poly(top_pts, color, sw))
            out.append(_poly(front_pts, color, sw))
        if o["name"] == "drill":
            for x, y in sorted({(round(m[1], 3), round(m[2], 3)) for m in o["moves"] if m[0] == 1}):
                out.append(f'<circle cx="{_n(ox + x)}" cy="{_n(oy_top - y)}" r="{_n(sw * 6)}" fill="none" '
                           f'stroke="{color}" stroke-width="{_n(sw * 2)}"/>')
        out.append(f'<text x="{_n(pad + i * (w / max(1, len(report["operations"]))))}" y="{_n(legend_y)}" '
                   f'font-size="{_n(sw * 11)}" fill="{color}">{o["label"].strip("_")} T{o["tool"]}</text>')
    out.append(f'<text x="{_n(pad)}" y="{_n(pad - sw * 4)}" font-size="{_n(sw * 11)}">top (XY), work zero at the '
               'lower-left corner of the stock</text>')
    out.append(f'<text x="{_n(pad)}" y="{_n(oy_front - sz1 - sw * 4)}" font-size="{_n(sw * 11)}">front (XZ), Z0 at '
               'the stock top</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _poly(pts, color, sw) -> str:
    return (f'<polyline points="{" ".join(f"{_n(x)},{_n(y)}" for x, y in pts)}" fill="none" stroke="{color}" '
            f'stroke-width="{_n(sw)}" stroke-opacity="0.8"/>')


def setup_sheet(spec: dict, report: dict, files: list[str], warnings: list[str], source: str) -> str:
    sx0, sy0, sz0, sx1, sy1, sz1 = report["stock"]
    st = spec["stock"]
    lines = [f"# Setup sheet: {Path(source).name}", "",
             f"Controller: {spec['controller']} (FreeCAD post `{spec['post']}`)"
             + (f", machine {spec['machine']['name']}" if spec["machine"].get("name") else ""), "",
             "## Stock and work zero", "",
             f"- Stock: {_n(sx1 - sx0)} x {_n(sy1 - sy0)} x {_n(sz1 - sz0)} mm "
             f"(part plus {_n(st['xy'])} mm each side, {_n(st['top'])} mm on top, {_n(st['bottom'])} mm below).",
             "- Work zero: X0 Y0 at the stock's lower-left (front-left) corner, Z0 on the stock top.",
             f"- Safe height {_n(spec['safe_height'])} mm, clearance {_n(spec['clearance_height'])} mm above the stock.",
             ]
    if st["bottom"] <= 0:
        lines.append(f"- The part sits on the bottom of the stock: cuts stop {_n(spec['skin'])} mm above it, so "
                     "the part stays attached; break it out and clean up the skin by hand.")
    lines += ["", "## Tools", "", "| T | Tool | Dia mm | RPM | Feed | Plunge | Stepdown |", "|---|---|---|---|---|---|---|"]
    for t in spec["tools"]:
        lines.append(f"| {t['number']} | {t['name']} | {_n(t['diameter'])} | {_n(t['rpm'])} | {_n(t['feed'])} | "
                     f"{_n(t['plunge'])} | {_n(t['stepdown']) if 'stepdown' in t else '-'} |")
    lines += ["", "## Operations (in order)", "", "| # | Operation | Tool | Est. time |", "|---|---|---|---|"]
    total = 0.0
    for i, o in enumerate(report["operations"], 1):
        total += _seconds(o["cycle_time"])
        lines.append(f"| {i} | {o['label'].strip('_')} | T{o['tool']} | {o['cycle_time']} |")
    lines += ["", f"Estimated machining time: about {total / 60:.0f} min (FreeCAD's estimate, without tool changes).",
              "", "## Files", ""] + [f"- `{f}`" for f in files]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    lines += ["", "## Before running", "",
              "- Open the `.FCStd` project in FreeCAD and run the CAM simulator, or simulate the G-code in CAMotics.",
              "- Check the tool lengths and set the tool length offset (or re-zero Z) after every tool change.",
              "- Dry run above the part first, with the feed override turned down.", ""]
    return "\n".join(lines)


def total_minutes(report: dict) -> float:
    return sum(_seconds(o["cycle_time"]) for o in report["operations"]) / 60


def mill(src: Path, out: Path, machine: dict, job: dict, freecad: list[str], *,
         runner: Optional[Callable] = None) -> dict:
    """STEP -> programs, preview, setup sheet and FreeCAD project next to ``out``; raises CamError."""
    spec = build_spec(machine, job)
    out.parent.mkdir(parents=True, exist_ok=True)
    project = out.with_suffix(".FCStd")
    spec.update({"input": str(src.resolve()), "output": str(out.resolve()), "save_project": str(project.resolve())})
    report = run_freecad(spec, freecad, runner=runner)
    errors, warnings = check(spec, report)
    files = [Path(f) for f in report.get("files") or []]
    if errors:
        for f in files:
            f.unlink(missing_ok=True)
        raise CamError("; ".join(errors) + (" | warnings: " + "; ".join(warnings) if warnings else ""))
    preview = out.with_suffix(".preview.svg")
    preview.write_text(preview_svg(report), "utf-8")
    sheet = out.with_suffix(".setup.md")
    names = [f.name for f in files] + [project.name]
    sheet.write_text(setup_sheet(spec, report, names, warnings, str(src)), "utf-8")
    return {"programs": [str(f) for f in files], "preview": str(preview), "sheet": str(sheet),
            "project": str(project) if project.is_file() else "", "warnings": warnings,
            "minutes": total_minutes(report), "operations": [o["label"].strip("_") for o in report["operations"]]}
