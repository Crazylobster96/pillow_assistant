"""2D cutting CAM: DXF profiles to laser / plasma G-code, standard library only.

Pipeline: read an ASCII DXF (LINE, ARC, CIRCLE, LWPOLYLINE, POLYLINE, ELLIPSE,
SPLINE), flatten curves to polylines, chain open pieces into loops, find each
loop's nesting depth, offset it by half the kerf away from the part, order the
cuts inside-out with nearest-neighbour travel, add lead-ins on the scrap side,
then emit G-code for the machine's controller dialect.  A preview SVG and a
summary go with every program.

Nothing here talks to a machine.  Every number that can hurt a machine or a
part (feed, power, pierce timing, kerf) comes from the caller's machine/job
settings; missing ones are an error, never a default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

Point = tuple[float, float]

CONTROLLERS = ("grbl", "linuxcnc", "fanuc", "generic")
PROCESSES = ("laser", "plasma")
INSUNITS_MM = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 0.0000254, 9: 0.0254, 10: 914.4}
EPS = 1e-9


class CamError(ValueError):
    """Bad input or settings; the message is meant for the user."""


# --------------------------------------------------------------------------- DXF


@dataclass
class Shape:
    points: list[Point]
    closed: bool
    layer: str = "0"


def _pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    out = []
    for i in range(0, len(lines) - 1, 2):
        try:
            out.append((int(lines[i].strip()), lines[i + 1].strip()))
        except ValueError:
            raise CamError(f"DXF is malformed near line {i + 1}") from None
    return out


def _arc_points(cx, cy, r, a0, a1, tol) -> list[Point]:
    """Points on a CCW arc from a0 to a1 (radians), chord error <= tol."""
    sweep = a1 - a0
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - tol / r))) if r > tol else math.pi / 4
    n = max(1, math.ceil(abs(sweep) / max(step, 1e-3)))
    return [(cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _bulge_points(p0: Point, p1: Point, bulge: float, tol: float) -> list[Point]:
    """Polyline segment with a DXF bulge (tan of a quarter of the included angle), without p0."""
    if abs(bulge) < 1e-12:
        return [p1]
    theta = 4 * math.atan(bulge)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    chord = math.hypot(dx, dy)
    if chord < EPS:
        return [p1]
    r = abs(chord / (2 * math.sin(theta / 2)))
    # The centre lies left of the chord for a CCW arc under 180 degrees; the signed tangent covers the rest.
    d = (chord / 2) / math.tan(theta / 2)
    cx = (p0[0] + p1[0]) / 2 - dy / chord * d
    cy = (p0[1] + p1[1]) / 2 + dx / chord * d
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    pts = _arc_points(cx, cy, r, a0, a0 + theta, tol)
    pts[-1] = p1
    return pts[1:]


def _bspline(degree: int, knots: list[float], ctrl: list[Point], weights: list[float], tol: float) -> list[Point]:
    n = len(ctrl)
    if n < degree + 1 or len(knots) != n + degree + 1:
        return list(ctrl)
    w = weights if len(weights) == n else [1.0] * n
    lo, hi = knots[degree], knots[n]

    def at(u: float) -> Point:
        k = degree
        for i in range(degree, n):
            if knots[i] <= u < knots[i + 1]:
                k = i
                break
        else:
            k = n - 1
        d = [(ctrl[j][0] * w[j], ctrl[j][1] * w[j], w[j]) for j in range(k - degree, k + 1)]
        for r in range(1, degree + 1):
            for j in range(degree, r - 1, -1):
                i = j + k - degree
                den = knots[i + degree + 1 - r] - knots[i]
                a = 0.0 if den == 0 else (u - knots[i]) / den
                d[j] = tuple((1 - a) * d[j - 1][c] + a * d[j][c] for c in range(3))
        x, y, ww = d[degree]
        return (x / ww, y / ww) if ww else (x, y)

    span = sum(math.dist(ctrl[i], ctrl[i + 1]) for i in range(n - 1))
    steps = max(8, min(4000, int(span / max(tol * 20, 1e-6))))
    return [at(lo + (hi - lo) * i / steps) for i in range(steps)] + [at(hi - 1e-12 * (hi - lo))]


def _through(fit: list[Point], tol: float) -> list[Point]:
    """A smooth curve through fit points (Catmull-Rom), for splines stored without control points."""
    if len(fit) < 3:
        return list(fit)
    ext = [fit[0]] + fit + [fit[-1]]
    out = [fit[0]]
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        n = max(2, min(200, int(math.dist(p1, p2) / max(tol * 20, 1e-6))))
        for k in range(1, n + 1):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * (2 * p1[c] + (p2[c] - p0[c]) * t + (2 * p0[c] - 5 * p1[c] + 4 * p2[c] - p3[c]) * t2
                                    + (3 * p1[c] - p0[c] - 3 * p2[c] + p3[c]) * t3) for c in range(2)))
    return out


def read_dxf(text: str, tol: float = 0.01) -> tuple[list[Shape], float, list[str]]:
    """Shapes in drawing units, the drawing unit in mm, and warnings."""
    if text.startswith("AutoCAD Binary DXF"):
        raise CamError("binary DXF is not supported; save it as ASCII DXF")
    pairs = _pairs(text)
    warnings: list[str] = []
    unit, section, i = 0, "", 0
    # Header: $INSUNITS.
    for j, (c, v) in enumerate(pairs):
        if c == 9 and v == "$INSUNITS" and j + 1 < len(pairs):
            try:
                unit = int(pairs[j + 1][1])
            except ValueError:
                unit = 0
            break
    scale = INSUNITS_MM.get(unit)
    if scale is None:
        scale = 1.0
        warnings.append("the DXF does not say its units ($INSUNITS); assumed millimetres")

    # Split into entities.
    entities: list[tuple[str, list[tuple[int, str]]]] = []
    while i < len(pairs):
        c, v = pairs[i]
        if c == 0 and v == "SECTION" and i + 1 < len(pairs):
            section = pairs[i + 1][1]
            i += 2
            continue
        if c == 0 and section == "ENTITIES" and v not in ("ENDSEC", "EOF"):
            j = i + 1
            while j < len(pairs) and pairs[j][0] != 0:
                j += 1
            entities.append((v, pairs[i + 1:j]))
            i = j
            continue
        i += 1

    shapes: list[Shape] = []
    skipped: dict[str, int] = {}
    k = 0
    while k < len(entities):
        kind, data = entities[k]
        k += 1
        g = {}
        for c, v in data:
            g.setdefault(c, v)
        layer = g.get(8, "0")
        flip = float(g.get(230, 1) or 1) < 0  # OCS extrusion (0,0,-1) mirrors X

        def f(code, default=0.0):
            try:
                return float(g.get(code, default))
            except ValueError:
                return default

        def fx(x: float) -> float:
            return -x if flip else x

        if kind == "LINE":
            shapes.append(Shape([(f(10), f(20)), (f(11), f(21))], False, layer))
        elif kind == "CIRCLE":
            pts = _arc_points(fx(f(10)), f(20), f(40), 0, 2 * math.pi, tol)[:-1]
            shapes.append(Shape(pts, True, layer))
        elif kind == "ARC":
            a0, a1 = math.radians(f(50)), math.radians(f(51))
            if flip:
                a0, a1 = math.pi - a1, math.pi - a0
            while a1 <= a0:
                a1 += 2 * math.pi
            shapes.append(Shape(_arc_points(fx(f(10)), f(20), f(40), a0, a1, tol), False, layer))
        elif kind in ("LWPOLYLINE", "POLYLINE"):
            verts: list[tuple[float, float, float]] = []
            closed = int(f(70)) & 1 == 1
            if kind == "LWPOLYLINE":
                for c, v in data:
                    if c == 10:
                        verts.append([float(v), 0.0, 0.0])
                    elif c == 20 and verts:
                        verts[-1][1] = float(v)
                    elif c == 42 and verts:
                        verts[-1][2] = float(v)
            else:
                while k < len(entities) and entities[k][0] == "VERTEX":
                    vg = {}
                    for c, v in entities[k][1]:
                        vg.setdefault(c, v)
                    verts.append([float(vg.get(10, 0)), float(vg.get(20, 0)), float(vg.get(42, 0))])
                    k += 1
                if k < len(entities) and entities[k][0] == "SEQEND":
                    k += 1
            if flip:
                verts = [[-x, y, -b] for x, y, b in verts]
            if not verts:
                continue
            pts: list[Point] = [(verts[0][0], verts[0][1])]
            seq = verts + ([verts[0]] if closed else [])
            for a, b in zip(seq, seq[1:]):
                pts.extend(_bulge_points((a[0], a[1]), (b[0], b[1]), a[2], tol))
            if closed:
                pts = pts[:-1]
            shapes.append(Shape(pts, closed, layer))
        elif kind == "ELLIPSE":
            cx, cy, mx, my, ratio = f(10), f(20), f(11), f(21), f(40, 1.0)
            t0, t1 = f(41, 0.0), f(42, 2 * math.pi)
            while t1 <= t0:
                t1 += 2 * math.pi
            a = math.hypot(mx, my)
            rot = math.atan2(my, mx)
            n = max(16, math.ceil((t1 - t0) / (2 * math.acos(max(-1.0, 1 - tol / max(a, tol))) or 0.1)))
            pts = []
            for s in range(n + 1):
                t = t0 + (t1 - t0) * s / n
                x, y = a * math.cos(t), a * ratio * math.sin(t)
                pts.append((fx(cx + x * math.cos(rot) - y * math.sin(rot)), cy + x * math.sin(rot) + y * math.cos(rot)))
            full = abs((t1 - t0) - 2 * math.pi) < 1e-6
            shapes.append(Shape(pts[:-1] if full else pts, full, layer))
        elif kind == "SPLINE":
            degree = int(f(71, 3))
            knots = [float(v) for c, v in data if c == 40]
            weights = [float(v) for c, v in data if c == 41]
            ctrl: list[Point] = []
            fit: list[Point] = []
            for c, v in data:
                if c == 10:
                    ctrl.append((float(v), 0.0))
                elif c == 20 and ctrl:
                    ctrl[-1] = (ctrl[-1][0], float(v))
                elif c == 11:
                    fit.append((float(v), 0.0))
                elif c == 21 and fit:
                    fit[-1] = (fit[-1][0], float(v))
            pts = _bspline(degree, knots, ctrl, weights, tol) if ctrl else _through(fit, tol)
            closed = int(f(70)) & 1 == 1 or (len(pts) > 2 and math.dist(pts[0], pts[-1]) < tol)
            if closed and len(pts) > 2 and math.dist(pts[0], pts[-1]) < tol:
                pts = pts[:-1]
            shapes.append(Shape(pts, closed, layer))
        elif kind in ("POINT", "TEXT", "MTEXT", "DIMENSION", "HATCH", "VERTEX", "SEQEND", "VIEWPORT"):
            if kind not in ("VERTEX", "SEQEND"):
                skipped[kind] = skipped.get(kind, 0) + 1
        else:
            skipped[kind] = skipped.get(kind, 0) + 1
    for kind, n in sorted(skipped.items()):
        hint = " (explode blocks before exporting)" if kind == "INSERT" else ""
        warnings.append(f"ignored {n} {kind} entit{'y' if n == 1 else 'ies'}{hint}")
    return shapes, scale, warnings


# --------------------------------------------------------------------------- geometry


def area(poly: list[Point]) -> float:
    s = 0.0
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        s += x0 * y1 - x1 * y0
    return s / 2


def length(path: list[Point], closed: bool = False) -> float:
    seq = path + path[:1] if closed else path
    return sum(math.dist(a, b) for a, b in zip(seq, seq[1:]))


def inside(pt: Point, poly: list[Point]) -> bool:
    x, y = pt
    hit = False
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            hit = not hit
    return hit


def _dedupe(pts: list[Point], tol: float, closed: bool) -> list[Point]:
    out: list[Point] = []
    for p in pts:
        if not out or math.dist(out[-1], p) > tol:
            out.append(p)
    if closed and len(out) > 1 and math.dist(out[0], out[-1]) <= tol:
        out.pop()
    return out


def chain(shapes: Iterable[Shape], tol: float) -> tuple[list[Shape], list[Shape]]:
    """Join open pieces end to end; returns (closed loops, still-open paths)."""
    loops: list[Shape] = []
    pieces: list[Shape] = []
    for s in shapes:
        if len(s.points) < 2:
            continue
        if s.closed:
            loops.append(Shape(_dedupe(s.points, tol / 10, True), True, s.layer))
        else:
            pieces.append(Shape(list(s.points), False, s.layer))
    opens: list[Shape] = []
    while pieces:
        cur = pieces.pop()
        pts = list(cur.points)
        grown = True
        while grown:
            grown = False
            if math.dist(pts[0], pts[-1]) <= tol and len(pts) > 2:
                break
            for idx, other in enumerate(pieces):
                o = other.points
                if math.dist(pts[-1], o[0]) <= tol:
                    pts += o[1:]
                elif math.dist(pts[-1], o[-1]) <= tol:
                    pts += o[-2::-1]
                elif math.dist(pts[0], o[-1]) <= tol:
                    pts = o[:-1] + pts
                elif math.dist(pts[0], o[0]) <= tol:
                    pts = o[:0:-1] + pts
                else:
                    continue
                pieces.pop(idx)
                grown = True
                break
        if len(pts) > 2 and math.dist(pts[0], pts[-1]) <= tol:
            loops.append(Shape(_dedupe(pts[:-1], tol / 10, True), True, cur.layer))
        else:
            opens.append(Shape(_dedupe(pts, tol / 10, False), False, cur.layer))
    return [s for s in loops if len(s.points) >= 3 and abs(area(s.points)) > tol * tol], opens


def offset_right(poly: list[Point], r: float, tol: float) -> list[Point]:
    """Offset a closed polygon to the right of its travel direction by r (round outer joins)."""
    if r <= 0:
        return list(poly)
    n = len(poly)
    out: list[Point] = []
    for i in range(n):
        p0, p, p1 = poly[i - 1], poly[i], poly[(i + 1) % n]
        e1 = (p[0] - p0[0], p[1] - p0[1])
        e2 = (p1[0] - p[0], p1[1] - p[1])
        l1, l2 = math.hypot(*e1), math.hypot(*e2)
        if l1 < EPS or l2 < EPS:
            continue
        n1 = (e1[1] / l1, -e1[0] / l1)
        n2 = (e2[1] / l2, -e2[0] / l2)
        cross = e1[0] * e2[1] - e1[1] * e2[0]
        turn = math.atan2(cross, e1[0] * e2[0] + e1[1] * e2[1])
        if turn > 0.05:  # the path turns left: the right side opens up, round it like the kerf does
            a0 = math.atan2(n1[1], n1[0])
            steps = max(1, math.ceil(turn / max(2 * math.acos(max(-1.0, 1 - tol / r)), 1e-3)))
            for s in range(steps + 1):
                a = a0 + turn * s / steps
                out.append((p[0] + r * math.cos(a), p[1] + r * math.sin(a)))
        else:
            d = 1 + n1[0] * n2[0] + n1[1] * n2[1]
            if d < 0.05:
                d = 0.05
            out.append((p[0] + r * (n1[0] + n2[0]) / d, p[1] + r * (n1[1] + n2[1]) / d))
    return out


def self_intersects(poly: list[Point]) -> bool:
    """Sweep over x for any two non-adjacent edges that cross."""
    n = len(poly)
    if n < 4:
        return False
    segs = []
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        segs.append((min(a[0], b[0]), max(a[0], b[0]), i, a, b))
    segs.sort()

    def ccw(p, q, s):
        return (q[0] - p[0]) * (s[1] - p[1]) - (q[1] - p[1]) * (s[0] - p[0])

    active: list = []
    for seg in segs:
        x0 = seg[0]
        active = [s for s in active if s[1] >= x0]
        for o in active:
            i, j = seg[2], o[2]
            if abs(i - j) in (0, 1, n - 1):
                continue
            a, b, c, d = seg[3], seg[4], o[3], o[4]
            if min(a[1], b[1]) > max(c[1], d[1]) or min(c[1], d[1]) > max(a[1], b[1]):
                continue
            d1, d2, d3, d4 = ccw(c, d, a), ccw(c, d, b), ccw(a, b, c), ccw(a, b, d)
            if d1 * d2 < -EPS and d3 * d4 < -EPS:
                return True
        active.append(seg)
    return False


# --------------------------------------------------------------------------- settings


@dataclass
class Settings:
    controller: str
    process: str
    kerf: float
    feed: float
    power: Optional[float] = None
    max_power: float = 1000.0
    laser_mode: str = "dynamic"
    pierce_delay: float = 0.0
    z_axis: bool = False
    safe_height: Optional[float] = None
    pierce_height: Optional[float] = None
    cut_height: Optional[float] = None
    plunge_feed: Optional[float] = None
    torch_on: Optional[str] = None
    torch_off: Optional[str] = None
    lead_in: float = 0.0
    overcut: float = 0.0
    origin: str = "drawing"
    bed: Optional[tuple[float, float]] = None
    rapid: float = 5000.0
    layers: list[str] = field(default_factory=list)
    engrave_layers: list[str] = field(default_factory=list)
    engrave_power: Optional[float] = None
    engrave_feed: Optional[float] = None
    tolerance: float = 0.01
    name: str = ""


def _num(d: dict, key: str, *, positive=False, nonneg=False) -> Optional[float]:
    if d.get(key) is None:
        return None
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        raise CamError(f"{key} must be a number") from None
    if not math.isfinite(v) or (positive and v <= 0) or (nonneg and v < 0):
        raise CamError(f"{key} must be {'> 0' if positive else '>= 0'}")
    return v


def _code(d: dict, key: str) -> Optional[str]:
    v = d.get(key)
    if v is None:
        return None
    v = str(v).strip()
    if not v or any(ch in v for ch in "\r\n;()%") or len(v) > 40:
        raise CamError(f"{key} must be a single short G/M-code line")
    return v


def settings(machine: dict, job: dict) -> Settings:
    """Merge machine + job settings; anything safety-relevant must be given, never guessed."""
    m = {**(machine or {}), **(job or {})}
    missing = [k for k in ("controller", "process", "kerf", "feed") if m.get(k) in (None, "")]
    process = str(m.get("process") or "").lower()
    if process == "laser" and m.get("power") is None:
        missing.append("power")
    if process == "plasma" and m.get("pierce_delay") is None:
        missing.append("pierce_delay")
    z_axis = bool(m.get("z_axis", False))
    if process == "plasma" and z_axis:
        missing += [k for k in ("safe_height", "pierce_height", "cut_height") if m.get(k) is None]
    if missing:
        raise CamError("missing machine/job settings: " + ", ".join(missing) +
                       " (ask the user; never guess feeds, power or pierce timing)")
    controller = str(m["controller"]).lower()
    if controller not in CONTROLLERS:
        raise CamError(f"controller must be one of {', '.join(CONTROLLERS)}")
    if process not in PROCESSES:
        raise CamError(f"process must be one of {', '.join(PROCESSES)}")
    s = Settings(controller=controller, process=process, kerf=_num(m, "kerf", nonneg=True),
                 feed=_num(m, "feed", positive=True), name=str(m.get("name") or ""))
    s.power = _num(m, "power", nonneg=True)
    s.max_power = _num(m, "max_power", positive=True) or s.max_power
    if s.power is not None and s.power > s.max_power:
        raise CamError(f"power {s.power:g} is above max_power {s.max_power:g}")
    mode = str(m.get("laser_mode") or "dynamic").lower()
    if mode not in ("dynamic", "constant"):
        raise CamError("laser_mode must be dynamic (M4) or constant (M3)")
    s.laser_mode = mode
    s.pierce_delay = _num(m, "pierce_delay", nonneg=True) or 0.0
    s.z_axis = z_axis
    s.safe_height = _num(m, "safe_height")
    s.pierce_height = _num(m, "pierce_height")
    s.cut_height = _num(m, "cut_height")
    s.plunge_feed = _num(m, "plunge_feed", positive=True)
    s.torch_on, s.torch_off = _code(m, "torch_on"), _code(m, "torch_off")
    s.lead_in = _num(m, "lead_in", nonneg=True) or 0.0
    s.overcut = _num(m, "overcut", nonneg=True) or 0.0
    s.origin = str(m.get("origin") or "drawing").lower()
    if s.origin not in ("drawing", "lower_left"):
        raise CamError("origin must be drawing or lower_left")
    bed = m.get("bed")
    if bed is not None:
        try:
            s.bed = (float(bed[0]), float(bed[1]))
        except (TypeError, ValueError, IndexError):
            raise CamError("bed must be [x, y] in mm") from None
    s.rapid = _num(m, "rapid", positive=True) or s.rapid
    s.layers = [str(x) for x in (m.get("layers") or [])]
    s.engrave_layers = [str(x) for x in (m.get("engrave_layers") or [])]
    s.engrave_power = _num(m, "engrave_power", nonneg=True)
    s.engrave_feed = _num(m, "engrave_feed", positive=True)
    if s.engrave_layers and process == "laser" and s.engrave_power is None:
        raise CamError("engrave_layers need engrave_power")
    if s.engrave_power is not None and s.engrave_power > s.max_power:
        raise CamError(f"engrave_power {s.engrave_power:g} is above max_power {s.max_power:g}")
    s.tolerance = _num(m, "tolerance", positive=True) or s.tolerance
    return s


# --------------------------------------------------------------------------- toolpath


@dataclass
class Cut:
    path: list[Point]          # in mm, already offset; closed paths repeat the start at the end
    lead: list[Point]          # lead-in from the pierce point to path[0] (may be empty)
    kind: str                  # outer, hole, open, engrave
    depth: int = 0


@dataclass
class Plan:
    cuts: list[Cut]
    warnings: list[str]
    settings: Settings


def _start_on_longest(path: list[Point]) -> list[Point]:
    """Rotate a closed path to start mid-way along its longest edge (away from corners)."""
    n = len(path)
    best = max(range(n), key=lambda i: math.dist(path[i], path[(i + 1) % n]))
    a, b = path[best], path[(best + 1) % n]
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return [mid] + path[best + 1:] + path[:best + 1]


def _walk(path: list[Point], dist: float) -> list[Point]:
    out, left = [path[0]], dist
    for a, b in zip(path, path[1:]):
        d = math.dist(a, b)
        if d >= left:
            t = left / d if d else 0
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            return out
        out.append(b)
        left -= d
    return out


def plan(shapes: list[Shape], scale: float, s: Settings, warnings: Optional[list[str]] = None) -> Plan:
    warnings = list(warnings or [])
    tol = s.tolerance
    shapes = [Shape([(x * scale, y * scale) for x, y in sh.points], sh.closed, sh.layer) for sh in shapes]
    if s.layers:
        keep = set(s.layers) | set(s.engrave_layers)
        shapes = [sh for sh in shapes if sh.layer in keep]
    engrave = [sh for sh in shapes if sh.layer in s.engrave_layers]
    shapes = [sh for sh in shapes if sh.layer not in s.engrave_layers]
    loops, opens = chain(shapes, max(tol * 5, 0.02))
    if not loops and not opens and not engrave:
        raise CamError("no cuttable geometry found in the DXF")

    polys = [lp.points for lp in loops]
    depth = [sum(1 for j, q in enumerate(polys) if j != i and abs(area(q)) > abs(area(p)) and inside(p[0], q))
             for i, p in enumerate(polys)]
    r = s.kerf / 2
    cuts: list[Cut] = []
    for poly, d in zip(polys, depth):
        hole = d % 2 == 1
        # Orient so the part is on the left, offset right (away from the part), then reverse
        # so the part is on the right of travel: outer clockwise, holes counter-clockwise.
        p = list(poly)
        if (area(p) > 0) == hole:
            p.reverse()
        off = offset_right(p, r, tol) if r > 0 else p
        # Offset vertices that come closer to the drawn edge than the kerf radius mean the cut would
        # eat into the part: a hole smaller than the kerf, or an inside corner tighter than it.
        closer = []
        if r > 0 and len(off) * len(p) <= 4_000_000:
            ring = p + p[:1]
            closer = [q for q in off if _clearance(q, ring) < r * 0.9]
        if hole and (len(off) < 3 or area(off) * area(p) <= 0 or len(closer) == len(off)
                     or abs(area(off)) < (tol * 10) ** 2):
            warnings.append(f"a hole near ({poly[0][0]:.2f}, {poly[0][1]:.2f}) is smaller than the kerf and was skipped")
            continue
        if closer or (r > 0 and self_intersects(off)):
            warnings.append(f"the kerf offset near ({poly[0][0]:.2f}, {poly[0][1]:.2f}) crosses itself: "
                            "a feature is narrower than the kerf; check the preview")
        off.reverse()
        path = _start_on_longest(off)
        cuts.append(Cut(path + [path[0]], [], "hole" if hole else "outer", d))
    for op in opens:
        cuts.append(Cut(op.points, [], "open", 0))
    if opens:
        warnings.append(f"{len(opens)} open path(s) do not close; they are cut on the line without kerf "
                        "compensation")
    eng_loops, eng_opens = chain(engrave, max(tol * 5, 0.02))
    for e in eng_loops:
        cuts.append(Cut(e.points + [e.points[0]], [], "engrave"))
    for e in eng_opens:
        cuts.append(Cut(e.points, [], "engrave"))

    # Lead-ins on the scrap side (left of travel).
    for c in cuts:
        if s.lead_in <= 0 or c.kind not in ("outer", "hole"):
            continue
        a, b = c.path[0], c.path[1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy) or 1
        left = (-dy / ln, dx / ln)
        L = s.lead_in
        while L > tol:
            q = (a[0] + left[0] * L, a[1] + left[1] * L)
            if c.kind == "outer" or inside(q, c.path[:-1]) and math.dist(q, a) < _clearance(q, c.path):
                c.lead = [q, a]
                break
            L /= 2
        if not c.lead:
            warnings.append(f"no room for a lead-in at ({a[0]:.2f}, {a[1]:.2f}); it pierces on the path")
        elif L < s.lead_in:
            warnings.append(f"lead-in shortened to {L:.2f} mm at ({a[0]:.2f}, {a[1]:.2f}) to fit the hole")

    if s.overcut > 0:
        for c in cuts:
            if c.kind in ("outer", "hole"):
                c.path = c.path + _walk(c.path, s.overcut)[1:]

    # Order: engraving first (parts must not move yet), then deepest loops first, nearest neighbour.
    ordered: list[Cut] = []
    pos: Point = (0.0, 0.0)
    groups = [[c for c in cuts if c.kind == "engrave"], [c for c in cuts if c.kind == "open"]]
    for d in sorted({c.depth for c in cuts if c.kind in ("outer", "hole")}, reverse=True):
        groups.append([c for c in cuts if c.kind in ("outer", "hole") and c.depth == d])
    for group in groups:
        rest = list(group)
        while rest:
            def start(c):
                return (c.lead or c.path)[0]
            nxt = min(rest, key=lambda c: math.dist(pos, start(c)))
            if nxt.kind in ("open", "engrave") and math.dist(pos, nxt.path[-1]) < math.dist(pos, nxt.path[0]) \
                    and nxt.path[0] != nxt.path[-1]:
                nxt.path.reverse()
            rest.remove(nxt)
            ordered.append(nxt)
            pos = nxt.path[-1]

    if s.origin == "lower_left" and ordered:
        xs = [p[0] for c in ordered for p in c.lead + c.path]
        ys = [p[1] for c in ordered for p in c.lead + c.path]
        mx, my = min(xs), min(ys)
        for c in ordered:
            c.path = [(x - mx, y - my) for x, y in c.path]
            c.lead = [(x - mx, y - my) for x, y in c.lead]
    return Plan(ordered, warnings, s)


def _clearance(q: Point, path: list[Point]) -> float:
    """Distance from q to the nearest edge of path."""
    best = math.inf
    for a, b in zip(path, path[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / L2))
        best = min(best, math.dist(q, (a[0] + dx * t, a[1] + dy * t)))
    return best + 1e-9


# --------------------------------------------------------------------------- output


def bounds(p: Plan) -> tuple[float, float, float, float]:
    pts = [q for c in p.cuts for q in c.lead + c.path]
    return min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts)


def stats(p: Plan) -> dict:
    s = p.settings
    cut = sum(length(c.lead) + length(c.path) for c in p.cuts if c.kind != "engrave")
    eng = sum(length(c.path) for c in p.cuts if c.kind == "engrave")
    rapid, pos = 0.0, (0.0, 0.0)
    for c in p.cuts:
        rapid += math.dist(pos, (c.lead or c.path)[0])
        pos = c.path[-1]
    pierces = len(p.cuts)
    minutes = cut / s.feed + eng / (s.engrave_feed or s.feed) + rapid / s.rapid + pierces * s.pierce_delay / 60
    return {"cut_mm": cut, "engrave_mm": eng, "rapid_mm": rapid, "pierces": pierces, "minutes": minutes,
            "holes": sum(c.kind == "hole" for c in p.cuts), "outers": sum(c.kind == "outer" for c in p.cuts),
            "open": sum(c.kind == "open" for c in p.cuts)}


def _n(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def gcode(p: Plan, source: str = "") -> str:
    s = p.settings
    fanuc = s.controller == "fanuc"
    lines: list[str] = []

    def comment(text: str) -> None:
        text = text.replace("(", "[").replace(")", "]")
        if fanuc:
            text = text.upper().encode("ascii", "replace").decode()
        lines.append(f"({text})")

    def dwell(sec: float) -> None:
        if sec > 0:
            lines.append(f"G4 X{_n(sec)}" if fanuc else f"G4 P{_n(sec)}")

    st = stats(p)
    x0, y0, x1, y1 = bounds(p)
    if fanuc:
        lines += ["%", "O0001"]
    comment(f"Pillow 2D {s.process} program for {s.controller}" + (f" / {s.name}" if s.name else ""))
    if source:
        comment(f"source {Path(source).name}")
    comment(f"kerf {_n(s.kerf)} mm, feed {_n(s.feed)} mm/min" +
            (f", power S{_n(s.power)}" if s.power is not None else "") +
            (f", pierce delay {_n(s.pierce_delay)} s" if s.pierce_delay else ""))
    comment(f"extents X{_n(x0)}..{_n(x1)} Y{_n(y0)}..{_n(y1)} mm, {st['pierces']} pierces, "
            f"about {st['minutes']:.1f} min")
    comment("REVIEW BEFORE RUNNING: check the preview, set work zero, dry run with the beam/torch off")
    lines.append("G21 G90 G94" if not fanuc else "G21 G90 G94 G17")
    if s.process == "plasma" and s.z_axis:
        lines.append(f"G0 Z{_n(s.safe_height)}")

    if s.process == "laser":
        on_word = "M4" if s.laser_mode == "dynamic" else "M3"
    else:
        on_word = "M3"
    on_code, off_code = s.torch_on or on_word, s.torch_off or "M5"

    for c in p.cuts:
        engrave = c.kind == "engrave"
        feed = (s.engrave_feed or s.feed) if engrave else s.feed
        power = s.engrave_power if engrave and s.engrave_power is not None else s.power
        comment(f"{c.kind}")
        start = (c.lead or c.path)[0]
        lines.append(f"G0 X{_n(start[0])} Y{_n(start[1])}")
        if s.process == "plasma" and s.z_axis:
            lines.append(f"G0 Z{_n(s.pierce_height)}")
        if s.process == "laser" and not s.torch_on:
            lines.append(f"{on_code} S{_n(power or 0)}")
        else:
            lines.append(on_code)
        dwell(s.pierce_delay)
        if s.process == "plasma" and s.z_axis:
            lines.append(f"G1 Z{_n(s.cut_height)} F{_n(s.plunge_feed or feed)}")
        first = True
        for q in (c.lead[1:] if c.lead else []) + c.path[1:]:
            lines.append(f"G1 X{_n(q[0])} Y{_n(q[1])}" + (f" F{_n(feed)}" if first else ""))
            first = False
        lines.append(off_code)
        if s.process == "plasma" and s.z_axis:
            lines.append(f"G0 Z{_n(s.safe_height)}")
    lines.append("M5")
    lines.append("M30" if fanuc else "M2")
    if fanuc:
        lines.append("%")
    return "\n".join(lines) + "\n"


def preview_svg(p: Plan) -> str:
    x0, y0, x1, y1 = bounds(p)
    pad = max(x1 - x0, y1 - y0) * 0.03 + 1
    w, h = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
    sw = max(w, h) / 600

    def pts(path):
        return " ".join(f"{_n(x)},{_n(y1 + y0 - y)}" for x, y in path)

    colors = {"outer": "#d62728", "hole": "#ff7f0e", "open": "#9467bd", "engrave": "#2ca02c"}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_n(x0 - pad)} {_n(y0 - pad)} {_n(w)} {_n(h)}" '
           f'width="{_n(min(1200, w * 4))}" style="background:#fff">']
    pos: Point = (0.0, 0.0)
    for i, c in enumerate(p.cuts, 1):
        start = (c.lead or c.path)[0]
        out.append(f'<polyline points="{pts([pos, start])}" fill="none" stroke="#999" stroke-width="{_n(sw)}" '
                   f'stroke-dasharray="{_n(sw * 4)}"/>')
        if c.lead:
            out.append(f'<polyline points="{pts(c.lead)}" fill="none" stroke="#1f77b4" stroke-width="{_n(sw * 2)}"/>')
        out.append(f'<polyline points="{pts(c.path)}" fill="none" stroke="{colors[c.kind]}" '
                   f'stroke-width="{_n(sw * 1.5)}"/>')
        out.append(f'<circle cx="{_n(start[0])}" cy="{_n(y1 + y0 - start[1])}" r="{_n(sw * 3)}" fill="#1f77b4"/>')
        out.append(f'<text x="{_n(start[0])}" y="{_n(y1 + y0 - start[1] - sw * 5)}" font-size="{_n(sw * 12)}" '
                   f'fill="#1f77b4">{i}</text>')
        pos = c.path[-1]
    out.append("</svg>")
    return "\n".join(out) + "\n"


def check_bed(p: Plan) -> Optional[str]:
    if not p.settings.bed or not p.cuts:
        return None
    x0, y0, x1, y1 = bounds(p)
    bx, by = p.settings.bed
    if x0 < -1e-6 or y0 < -1e-6 or x1 > bx + 1e-6 or y1 > by + 1e-6:
        return (f"toolpath X{_n(x0)}..{_n(x1)} Y{_n(y0)}..{_n(y1)} mm does not fit the {_n(bx)} x {_n(by)} mm bed "
                "(try origin=lower_left, or move the drawing)")
    return None


def cut2d(dxf_text: str, machine: dict, job: dict, source: str = "") -> tuple[str, str, dict, list[str]]:
    """DXF text -> (G-code, preview SVG, stats, warnings); raises CamError."""
    s = settings(machine, job)
    shapes, scale, warnings = read_dxf(dxf_text, tol=s.tolerance)
    p = plan(shapes, scale, s, warnings)
    if not p.cuts:
        raise CamError("nothing left to cut after filtering: " + "; ".join(p.warnings))
    problem = check_bed(p)
    if problem:
        raise CamError(problem)
    return gcode(p, source), preview_svg(p), stats(p), p.warnings
