"""3+2 / 4th-axis indexed milling: several 3-axis setups, each with the part turned by rotary axes.

Each setup names the direction the tool comes from (``+Y``, ``-X``, a vector) or
the rotary angles directly.  Pillow works out the rotation, FreeCAD machines the
part turned into that frame (``cam_freecad_runner.place_indexed``), and the
program of each setup gets a block that retracts and then either turns the
table (``table`` mode: ``G0 A90``) or tilts the work plane on the control
(``tilted_plane`` mode, Fanuc ``G68.2`` / ``G53.1`` / ``G69``, which also suits
head-tilting machines).

Work zero is the rotary centre.  A axis: the part's centre line on the axis, X0 at
the chuck jaw face and the stub held in the chuck from X-hold to X0.  B axis: the same along Y.  AC / BC trunnion: the
part centred on the C table, whose top is ``center_height`` below the tilt axis.
The positive direction of each axis is the right-hand rule turning the part;
``signs`` flips an axis whose machine runs the other way.
"""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Callable, Optional

from pillow_assistant.capabilities import cam_mill
from pillow_assistant.capabilities.cam2d import CamError, _n

AXES = ("A", "B", "AC", "BC")
MODES = ("table", "tilted_plane")
DIRECTIONS = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0),
              "+Z": (0, 0, 1), "-Z": (0, 0, -1)}
ROTARY_CONTROLLERS = {"linuxcnc", "fanuc", "mach3", "mach4", "centroid", "uccnc", "heidenhain", "grbl"}

Matrix = list[list[float]]


def rx(a: float) -> Matrix:
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def ry(a: float) -> Matrix:
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def rz(a: float) -> Matrix:
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def mul(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def apply(m: Matrix, v) -> tuple[float, float, float]:
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def rotation(axes: str, angles: dict) -> Matrix:
    """The part's rotation for table axes: C turns the part first, then A or B tilts it."""
    a, b, c = (float(angles.get(k, 0.0)) for k in ("A", "B", "C"))
    return {"A": rx(a), "B": ry(b), "AC": mul(rx(a), rz(c)), "BC": mul(ry(b), rz(c))}[axes]


def angles_for(axes: str, d) -> dict:
    """Rotary angles that turn direction ``d`` (from the part towards the tool) to +Z."""
    dx, dy, dz = d
    if axes == "A":
        if abs(dx) > 1e-6:
            raise CamError(f"an A axis cannot point the tool along {_vec(d)}: it needs no X component")
        return {"A": math.degrees(math.atan2(dy, dz))}
    if axes == "B":
        if abs(dy) > 1e-6:
            raise CamError(f"a B axis cannot point the tool along {_vec(d)}: it needs no Y component")
        return {"B": math.degrees(math.atan2(-dx, dz))}
    s = math.hypot(dx, dy)
    if axes == "AC":
        c = math.degrees(math.atan2(dx, dy)) if s > 1e-9 else 0.0
        return {"A": math.degrees(math.atan2(s, dz)), "C": c}
    c = math.degrees(math.atan2(-dy, dx)) if s > 1e-9 else 0.0
    return {"B": math.degrees(math.atan2(-s, dz)), "C": c}


def _vec(d) -> str:
    return "[" + ", ".join(_n(v) for v in d) + "]"


def direction(value) -> tuple[float, float, float]:
    if isinstance(value, str):
        key = value.strip().upper()
        if key in ("X", "Y", "Z"):
            key = "+" + key
        if key not in DIRECTIONS:
            raise CamError(f"direction must be one of {', '.join(DIRECTIONS)} or [x, y, z]")
        return DIRECTIONS[key]
    try:
        v = [float(x) for x in value][:3]
        n = math.sqrt(sum(x * x for x in v))
        if len(v) != 3 or n < 1e-9:
            raise ValueError
    except (TypeError, ValueError):
        raise CamError("direction must be one of +X, -X, +Y, -Y, +Z, -Z or a vector [x, y, z]") from None
    return tuple(x / n for x in v)


def euler_zxz(r: Matrix) -> tuple[float, float, float]:
    """G68.2 I J K (ZXZ Euler angles) of the tilted frame whose axes are the rows of ``r``."""
    m = [[r[j][i] for j in range(3)] for i in range(3)]  # frame axes as columns
    beta = math.degrees(math.acos(max(-1.0, min(1.0, m[2][2]))))
    if abs(math.sin(math.radians(beta))) < 1e-9:
        return math.degrees(math.atan2(m[1][0], m[0][0])), beta, 0.0
    alpha = math.degrees(math.atan2(m[0][2], -m[1][2]))
    gamma = math.degrees(math.atan2(m[2][0], m[2][1]))
    # Rz(a)Rx(b)Rz(c) == Rz(a+180)Rx(-b)Rz(c+180): give the operator the smaller-looking one.
    alt = (_wrap(alpha + 180), -beta, _wrap(gamma + 180))
    if abs(alt[0]) + abs(alt[2]) < abs(alpha) + abs(gamma) - 1e-9:
        return alt
    return alpha, beta, gamma


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def rotary_settings(machine: dict) -> dict:
    r = machine.get("rotary")
    if not isinstance(r, dict) or not r.get("axes"):
        raise CamError("setups need machine.rotary {axes: A | B | AC | BC, ...} (ask the user which rotary "
                       "axes the machine has)")
    axes = str(r["axes"]).upper()
    if axes not in AXES:
        raise CamError(f"machine.rotary.axes must be one of {', '.join(AXES)}")
    mode = str(r.get("mode") or "table").lower()
    if mode not in MODES:
        raise CamError(f"machine.rotary.mode must be one of {', '.join(MODES)}")
    controller = str(machine.get("controller") or "").lower()
    if mode == "tilted_plane" and controller != "fanuc":
        raise CamError("tilted_plane mode writes Fanuc G68.2; use mode table for other controllers")
    limits = {}
    for k, v in (r.get("limits") or {}).items():
        try:
            limits[k.upper()] = (float(v[0]), float(v[1]))
        except (TypeError, ValueError, IndexError):
            raise CamError("machine.rotary.limits must look like {\"A\": [-120, 120]}") from None
    signs = {k.upper(): (-1.0 if float(v) < 0 else 1.0) for k, v in (r.get("signs") or {}).items()}
    retract = str(r.get("retract") or "G53 G0 Z0").strip()
    if not retract or any(ch in retract for ch in "\r\n;()%") or len(retract) > 40:
        raise CamError("machine.rotary.retract must be a single short G-code line")
    center = float(r.get("center_height") or 0.0)
    hold = float(r.get("hold") or 0.0)
    if hold < 0 or not math.isfinite(center):
        raise CamError("machine.rotary.hold must be >= 0 and center_height a number")
    return {"axes": axes, "mode": mode, "limits": limits, "signs": signs, "retract": retract,
            "center_height": center, "hold": hold}


def plan_setups(machine: dict, job: dict) -> tuple[dict, list[dict]]:
    """(rotary settings, setups each with rotation, machine angles, operations); raises CamError."""
    rot = rotary_settings(machine)
    base = cam_mill.build_spec(machine, {k: v for k, v in job.items() if k != "setups"})
    by_number = {t["number"]: t for t in base["tools"]}
    allowance = job.get("finish_allowance")
    allowance = 0.3 if allowance is None else float(allowance)
    out = []
    for i, s in enumerate(job["setups"], 1):
        if not isinstance(s, dict):
            raise CamError(f"setup {i} must be an object")
        given = {k.upper(): float(v) for k, v in s.items() if k.upper() in ("A", "B", "C") and v is not None}
        if given and s.get("direction") is not None:
            raise CamError(f"setup {i}: give either direction or angles, not both")
        if given:
            extra = set(given) - set(rot["axes"])
            if extra:
                raise CamError(f"setup {i}: the machine has no {', '.join(sorted(extra))} axis")
            angles = {k: given.get(k, 0.0) for k in rot["axes"]}
            r = rotation(rot["axes"], angles)
            tool_dir = apply([[r[j][i2] for j in range(3)] for i2 in range(3)], (0, 0, 1))
        elif s.get("direction") is not None:
            tool_dir = direction(s["direction"])
            axes = rot["axes"] if rot["mode"] == "table" else "AC"
            angles = angles_for(axes, tool_dir)
            r = rotation(axes, angles)
            if rot["mode"] == "tilted_plane":
                angles = {}
        else:
            raise CamError(f"setup {i}: give direction (+X, -Y, …) or the rotary angles")
        check = apply(r, tool_dir)
        if abs(check[2] - 1) > 1e-6:
            raise CamError(f"setup {i}: internal rotation error")
        machine_angles = {k: _round(v * rot["signs"].get(k, 1.0)) for k, v in angles.items()}
        for k, v in machine_angles.items():
            lo, hi = rot["limits"].get(k, (-math.inf, math.inf))
            if not lo - 1e-6 <= v <= hi + 1e-6:
                raise CamError(f"setup {i}: {k}{_n(v)} is outside the {k} axis limits {_n(lo)}..{_n(hi)}")
        ops = s.get("operations")
        where = f"setup {i} "
        checked = (cam_mill.check_operations(ops, by_number, allowance, where) if ops
                   else base["operations"])
        floor = s.get("floor")
        if floor is None and rot["axes"] in ("A", "B"):
            floor = 0.0  # down to the rotary axis; the other setups do the other side
        out.append({"index": i, "name": str(s.get("name") or f"S{i}")[:40], "rotation": r,
                    "angles": machine_angles, "direction": [_round(v) for v in tool_dir],
                    "operations": checked, "floor": None if floor is None else float(floor)})
    return {**rot, "base": base}, out


def _round(v: float) -> float:
    v = round(v, 6)
    return 0.0 if v == 0 else v


def _fmt_angles(angles: dict) -> str:
    return " ".join(f"{k}{v:.3f}" for k, v in angles.items())


_UNITS = re.compile(r"^(N\d+\s+)?([GM]\d+(\.\d)?\s+)*G2[01]\b")
_TOOL_CHANGE = re.compile(r"^(N\d+\s+)?(T\d+\s+)?M0?6\b")
_END = re.compile(r"^(N\d+\s+)?M(0?2|30)\b")


def add_rotary_blocks(text: str, controller: str, rot: dict, setup: dict, count: int) -> str:
    """Add this setup's rotary positioning to one program file.

    Table mode: retract and turn the axes once, right after the units line and
    before the first tool change, so the spindle is stopped while the part turns.
    Tilted-plane mode (Fanuc): tool changes are not allowed in a tilted plane, so
    every tool change is wrapped: G69 before it, and retract + G68.2 + G53.1 after
    the post's tool-length (G43) line, which may also select G54; G69 again before
    the program end.
    """
    label = f"SETUP {setup['index']}/{count} {setup['name']}"
    if rot["mode"] == "table":
        block = [f"({label}: {_fmt_angles(setup['angles'])})", rot["retract"],
                 "G90 G0 " + _fmt_angles(setup["angles"])]
    else:
        i, j, k = (_round(v) for v in euler_zxz(setup["rotation"]))
        block = [f"({label}: TILTED PLANE)", rot["retract"], "G90",
                 f"G68.2 X0 Y0 Z0 I{i:.3f} J{j:.3f} K{k:.3f}", "G53.1"]
    if controller == "fanuc":
        block = [b.upper() for b in block]
    lines = text.splitlines()
    out: list[str] = []
    if rot["mode"] == "table":
        at = next((n for n, line in enumerate(lines) if _UNITS.match(line.strip().upper())), None)
        if at is None:
            at = next((n - 1 for n, line in enumerate(lines) if _TOOL_CHANGE.match(line.strip().upper())), None)
        if at is None:
            at = 0 if lines and lines[0].strip() == "%" else -1
        return "\n".join(lines[:at + 1] + block + lines[at + 1:]) + "\n"
    placed = pending = False
    for line in lines:
        bare = line.strip().upper()
        if pending and bare and not bare.startswith("("):
            # Tilt after the post's tool-length line (G43, which may also select G54), else right away.
            if re.search(r"\bG4[34]\b", bare):
                out.append(line)
                out.extend(block)
                pending = False
                continue
            out.extend(block)
            pending = False
        if _TOOL_CHANGE.match(bare) or _END.match(bare):
            if pending:
                out.extend(block)
                pending = False
            out.append("G69")
        out.append(line)
        if _TOOL_CHANGE.match(bare):
            pending = placed = True
    if not placed:
        raise CamError("the program has no tool change (M6) to set the tilted plane after")
    return "\n".join(out) + "\n"


def swing_radius(axes: str, reports: list[dict]) -> float:
    """How far the stock reaches from the rotary centre, the room it needs to turn (bounding-box estimate)."""
    best = 0.0
    for r in reports:
        x0, y0, z0, x1, y1, z1 = r["stock"]
        for x in (x0, x1):
            for y in (y0, y1):
                for z in (z0, z1):
                    d = {"A": math.hypot(y, z), "B": math.hypot(x, z)}.get(axes, math.sqrt(x * x + y * y + z * z))
                    best = max(best, d)
    return best


def mill_indexed(src: Path, out: Path, machine: dict, job: dict, freecad: list[str], *,
                 runner: Optional[Callable] = None) -> dict:
    rot, setups = plan_setups(machine, job)
    base = rot["base"]
    hold = float(job.get("hold", rot["hold"]) or 0.0)
    if hold < 0:
        raise CamError("job.hold must be >= 0")
    rot["hold"] = hold
    offset = job.get("offset")
    if offset is not None:
        try:
            offset = [float(v) for v in offset]
        except (TypeError, ValueError):
            offset = []
        if len(offset) != 3:
            raise CamError("job.offset must be [x, y, z] in mm")
    warnings: list[str] = []
    if rot["mode"] == "table" and base["controller"] == "grbl":
        warnings.append("standard GRBL drives 3 axes; the A/B/C moves need grblHAL or a 4/5-axis GRBL build")
    if rot["axes"] in ("A", "B") and not hold:
        warnings.append("job.hold is 0, so no stock is left for the chuck to grip; set it to the length "
                        "held in the chuck")
    out.parent.mkdir(parents=True, exist_ok=True)
    programs, previews, projects, sections = [], [], [], []
    minutes = 0.0
    written: list[Path] = []
    try:
        for s in setups:
            spec = copy.deepcopy(base)
            stem = f"{out.stem}_S{s['index']}"
            target = out.with_name(stem + out.suffix)
            project = out.with_name(stem + ".FCStd")
            spec.update({"input": str(src.resolve()), "output": str(target.resolve()),
                         "save_project": str(project.resolve()), "operations": s["operations"],
                         "indexed": {"mount": rot["axes"] if rot["axes"] in ("A", "B") else "table",
                                     "rotation": s["rotation"], "hold": rot["hold"],
                                     "center_height": rot["center_height"], "floor": s["floor"],
                                     "skin": base["skin"], "offset": offset}})
            report = cam_mill.run_freecad(spec, freecad, runner=runner)
            files = [Path(f) for f in report.get("files") or []]
            written += files
            errors, warns = cam_mill.check(spec, report)
            if errors:
                raise CamError(f"setup {s['index']}: " + "; ".join(errors))
            for f in files:
                f.write_text(add_rotary_blocks(f.read_text("utf-8"), base["controller"], rot, s, len(setups)),
                             "utf-8", newline="\n")
            preview = out.with_name(stem + ".preview.svg")
            preview.write_text(cam_mill.preview_svg(report), "utf-8")
            programs += [str(f) for f in files]
            previews.append(str(preview))
            if project.is_file():
                projects.append(str(project))
            warnings += [f"setup {s['index']}: {w}" for w in warns]
            minutes += cam_mill.total_minutes(report)
            sections.append((s, report, [f.name for f in files]))
    except CamError:
        for f in written:
            f.unlink(missing_ok=True)
        raise
    radius = swing_radius(rot["axes"], [r for _, r, _ in sections])
    rot["swing"] = radius
    warnings.append(f"the stock reaches {radius:.0f} mm from the rotary centre: before each rotation the tool "
                    f"tip must be more than {radius:.0f} mm above the centre (`{rot['retract']}` must get it there)")
    warnings.append("check the rotary direction on the machine before cutting: the program assumes the "
                    "right-hand rule turning the part (set machine.rotary.signs if yours runs the other way)")
    sheet = out.with_suffix(".setup.md")
    sheet.write_text(indexed_sheet(base, rot, sections, warnings, str(src), minutes), "utf-8")
    return {"programs": programs, "preview": previews[0], "previews": previews, "sheet": str(sheet),
            "project": projects[0] if projects else "", "projects": projects, "warnings": warnings,
            "minutes": minutes,
            "operations": [f"S{s['index']} {s['name']}: " + _fmt_angles(s["angles"]) if s["angles"]
                           else f"S{s['index']} {s['name']}: tilted {_vec(s['direction'])}" for s in setups]}


def indexed_sheet(base: dict, rot: dict, sections: list, warnings: list[str], source: str, minutes: float) -> str:
    st = base["stock"]
    mount = {"A": "the part's centre line on the A axis, X0 at the chuck jaw face (the stub runs into the "
                  "chuck along -X)",
             "B": "the part's centre line on the B axis, Y0 at the chuck jaw face (the stub runs into the "
                  "chuck along -Y)",
             "AC": f"the part centred on the C table, table top {_n(rot['center_height'])} mm below the A axis",
             "BC": f"the part centred on the C table, table top {_n(rot['center_height'])} mm below the B axis"}
    lines = [f"# Setup sheet (indexed {rot['axes']}, {rot['mode']}): {Path(source).name}", "",
             f"Controller: {base['controller']} (FreeCAD post `{base['post']}`)", "",
             "## Work zero and stock", "",
             "- Work zero: the rotary centre (where the rotary axes cross). " + mount[rot["axes"]] + ".",
             f"- Stock margins: {_n(st['xy'])} mm around, {_n(st['top'])} mm on top, {_n(st['bottom'])} mm below"
             + (f", plus a {_n(rot['hold'])} mm stub held by the chuck (never cut)." if rot["hold"] else "."),
             f"- Before every rotation the tool retracts with `{rot['retract']}`. The stock reaches "
             f"{rot['swing']:.0f} mm from the rotary centre, so the tool tip must be higher than that.", "",
             "## Tools", "", "| T | Tool | Dia mm | RPM | Feed | Plunge | Stepdown |", "|---|---|---|---|---|---|---|"]
    for t in base["tools"]:
        lines.append(f"| {t['number']} | {t['name']} | {_n(t['diameter'])} | {_n(t['rpm'])} | {_n(t['feed'])} | "
                     f"{_n(t['plunge'])} | {_n(t['stepdown']) if 'stepdown' in t else '-'} |")
    for s, report, files in sections:
        where = _fmt_angles(s["angles"]) if s["angles"] else "tilted plane " + "I%.3f J%.3f K%.3f" % euler_zxz(s["rotation"])
        lines += ["", f"## Setup {s['index']}: {s['name']} ({where}, tool from {_vec(s['direction'])})", "",
                  "| # | Operation | Tool | Est. time |", "|---|---|---|---|"]
        for i, o in enumerate(report["operations"], 1):
            lines.append(f"| {i} | {o['label'].strip('_')} | T{o['tool']} | {o['cycle_time']} |")
        lines += ["", "Files: " + ", ".join(f"`{f}`" for f in files)]
    lines += ["", f"Estimated machining time: about {minutes:.0f} min (without tool changes and rotations)."]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    lines += ["", "## Before running", "",
              "- Open each setup's `.FCStd` project in FreeCAD and run the CAM simulator.",
              "- Check that the rotary turns the way the program expects with a slow test move.",
              "- Make sure the stock and fixture clear the spindle at every angle; dry run each setup.", ""]
    return "\n".join(lines)
