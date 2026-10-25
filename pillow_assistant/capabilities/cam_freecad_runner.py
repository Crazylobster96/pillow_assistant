"""Runs inside FreeCAD (``freecadcmd``), not inside Pillow: builds a CAM job and posts G-code.

Pillow (``cam_mill.py``) validates the user's settings and writes a spec JSON;
its path is passed in ``PILLOW_CAM_SPEC``.  This script imports the STEP model,
moves it so work zero is the stock's top / lower-left corner, creates the
stock, tool controllers and operations, posts the program with FreeCAD's
post-processor for the controller, and writes a report JSON with the toolpath
moves so Pillow can check them and draw a preview.
"""

import json
import math
import os
import sys
import traceback


def main():
    spec = json.load(open(os.environ["PILLOW_CAM_SPEC"], encoding="utf-8"))
    report = {"ok": False, "warnings": [], "operations": [], "files": []}
    try:
        run(spec, report)
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001 - everything goes back to Pillow in the report
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-4000:]
    with open(spec["report"], "w", encoding="utf-8") as fh:
        json.dump(report, fh)


def run(spec, report):
    import FreeCAD
    import Import
    import Path
    import Path.Main.Job as PathJob
    import Path.Main.Stock as PathStock
    import Path.Tool.Controller as PathToolController
    from Path.Post.Processor import PostProcessorFactory
    from Path.Tool.toolbit import ToolBit

    Path.Log.setLevel(Path.Log.Level.ERROR)
    doc = FreeCAD.newDocument("PillowCAM")
    before = set(o.Name for o in doc.Objects)
    Import.insert(spec["input"], doc.Name)
    solids = [o for o in doc.Objects if o.Name not in before and hasattr(o, "Shape")
              and o.Shape.Solids and not o.InList]
    if not solids:
        raise ValueError("the STEP file has no solid")
    if len(solids) > 1:
        import Part
        fused = doc.addObject("Part::Feature", "Model")
        fused.Shape = Part.makeCompound([o.Shape for o in solids])
        for o in solids:
            o.Visibility = False
        model = fused
        report["warnings"].append(f"the STEP file has {len(solids)} solids; they are machined as one part")
    else:
        model = solids[0]
    doc.recompute()

    # optimalBoundingBox is tight on curved faces; BoundBox can be several mm loose there.
    bb = model.Shape.optimalBoundingBox()
    st = spec["stock"]
    sx0, sy0 = bb.XMin - st["xy"], bb.YMin - st["xy"]
    sz_top = bb.ZMax + st["top"]
    # Work zero: stock lower-left corner in XY, stock top in Z.
    shift = FreeCAD.Vector(-sx0, -sy0, -sz_top)
    model.Placement = FreeCAD.Placement(model.Placement.Base + shift, model.Placement.Rotation)
    doc.recompute()
    bb = model.Shape.optimalBoundingBox()
    report["model"] = [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]

    job = PathJob.Create("Job", [model])
    clone = job.Model.Group[0]
    if job.Stock:
        doc.removeObject(job.Stock.Name)
    size = FreeCAD.Vector(bb.XLength + 2 * st["xy"], bb.YLength + 2 * st["xy"], bb.ZLength + st["top"] + st["bottom"])
    job.Stock = PathStock.CreateBox(job, size, FreeCAD.Placement(FreeCAD.Vector(0, 0, -size.z), FreeCAD.Rotation()))
    doc.recompute()
    report["stock"] = [0.0, 0.0, -size.z, size.x, size.y, 0.0]

    ss = job.SetupSheet
    ss.SafeHeightOffset = f"{spec['safe_height']} mm"
    ss.ClearanceHeightOffset = f"{spec['clearance_height']} mm"
    job.GeometryTolerance = f"{spec.get('tolerance', 0.01)} mm"

    controllers = {}
    for t in spec["tools"]:
        bit = ToolBit.from_shape_id(f"{t['shape']}.fcstd", label=t["name"])
        tool = bit.attach_to_doc(doc=doc)
        tool.Diameter = f"{t['diameter']} mm"
        if t.get("length"):
            tool.Length = f"{t['length']} mm"
        if t.get("flutes") and hasattr(tool, "Flutes"):
            tool.Flutes = int(t["flutes"])
        if t.get("flute_length") and hasattr(tool, "CuttingEdgeHeight"):
            tool.CuttingEdgeHeight = f"{t['flute_length']} mm"
        if t.get("tip_angle") and hasattr(tool, "TipAngle"):
            tool.TipAngle = f"{t['tip_angle']} deg"
        tc = PathToolController.Create(f"T{t['number']} {t['name']}", tool, t["number"], False)
        tc.SpindleSpeed = float(t["rpm"])
        tc.SpindleDir = "Forward"
        tc.HorizFeed = f"{t['feed']} mm/min"
        tc.VertFeed = f"{t['plunge']} mm/min"
        if spec.get("rapid"):
            tc.HorizRapid = f"{spec['rapid']} mm/min"
            tc.VertRapid = f"{spec['rapid']} mm/min"
        job.Proxy.addToolController(tc)
        controllers[t["number"]] = tc
    for tc in list(job.Tools.Group):
        if tc not in controllers.values():
            job.Tools.removeObject(tc)
            doc.removeObject(tc.Name)
    doc.recompute()

    floor = report["model"][2]
    if st["bottom"] <= 0:
        floor += spec["skin"]  # leave a skin so a through-cut part stays attached to the sheet

    # Headless FreeCAD cannot ask which tool controller a new operation gets when there are
    # several (findToolController falls through); give it the first and set the right one after.
    import PathScripts.PathUtils as PathUtils
    PathUtils.findToolController = lambda obj, proxy, name=None: PathUtils.getToolControllers(obj, proxy)[0]

    ops = []
    for i, o in enumerate(spec["operations"], 1):
        op = make_op(o, i, controllers[o["tool"]], clone, report["stock"][5], floor, report)
        if op is not None:
            ops.append((o, op))
    doc.recompute()

    for o, op in ops:
        moves = []
        last = {"X": 0.0, "Y": 0.0, "Z": report["stock"][5] + spec["clearance_height"]}
        for cmd in op.Path.Commands:
            name = cmd.Name.upper()
            if name in ("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"):
                p = cmd.Parameters
                pt = {k: p.get(k, last[k]) for k in ("X", "Y", "Z")}
                if name in ("G2", "G02", "G3", "G03"):
                    moves.extend(arc_moves(last, pt, p, name in ("G2", "G02")))
                else:
                    moves.append([0 if name in ("G0", "G00") else 1, pt["X"], pt["Y"], pt["Z"]])
                last = pt
            elif name in ("G81", "G82", "G83", "G73"):
                p = cmd.Parameters
                x, y = p.get("X", last["X"]), p.get("Y", last["Y"])
                r = p.get("R", last["Z"])
                moves += [[0, x, y, last["Z"]], [0, x, y, r], [1, x, y, p.get("Z", r)], [0, x, y, r]]
                last = {"X": x, "Y": y, "Z": r}
        cycle = getattr(op, "CycleTime", "")
        report["operations"].append({"name": o["type"], "label": op.Label, "tool": o["tool"],
                                     "moves": moves, "cycle_time": str(cycle),
                                     "empty": not any(m[0] == 1 for m in moves)})
        if not any(m[0] == 1 for m in moves):
            report["warnings"].append(f"operation {op.Label} produced no cutting moves")

    job.PostProcessor = spec["post"]
    job.PostProcessorArgs = spec.get("post_args", "")
    job.SplitOutput = bool(spec.get("split_by_tool"))
    job.OrderOutputBy = "Tool"
    doc.recompute()
    post = PostProcessorFactory.get_post_processor(job, spec["post"])
    sections = post.export() or []
    base, ext = os.path.splitext(spec["output"])
    if len(sections) == 1 or not job.SplitOutput:
        files = [(spec["output"], "\n".join(g for _, g in sections if g))]
    else:
        # FreeCAD starts a new section at every tool change, in operation order.
        runs = []
        for o, _ in ops:
            if not runs or runs[-1] != o["tool"]:
                runs.append(o["tool"])
        files = [(f"{base}_{n:02d}_T{tool}{ext}", gcode)
                 for n, ((_, gcode), tool) in enumerate(zip(sections, runs), 1)]
        if len(runs) != len(sections):
            raise RuntimeError(f"post-processor returned {len(sections)} sections for {len(runs)} tool runs")
    for path, text in files:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        report["files"].append(path)
    if spec.get("save_project"):
        doc.saveAs(spec["save_project"])
        report["project"] = spec["save_project"]


def arc_moves(a, b, p, cw):
    """Flatten a G2/G3 arc (XY plane, helical Z allowed) into feed moves."""
    cx, cy = a["X"] + p.get("I", 0.0), a["Y"] + p.get("J", 0.0)
    r = math.hypot(a["X"] - cx, a["Y"] - cy)
    a0 = math.atan2(a["Y"] - cy, a["X"] - cx)
    a1 = math.atan2(b["Y"] - cy, b["X"] - cx)
    sweep = a1 - a0
    if cw and sweep >= 0:
        sweep -= 2 * math.pi
    if not cw and sweep <= 0:
        sweep += 2 * math.pi
    n = max(2, int(abs(sweep) * max(r, 0.1) / 0.5))
    out = []
    for i in range(1, n + 1):
        t = i / n
        ang = a0 + sweep * t
        out.append([1, cx + r * math.cos(ang), cy + r * math.sin(ang), a["Z"] + (b["Z"] - a["Z"]) * t])
    return out


def _set(op, prop, value):
    """Set a property, dropping the expression FreeCAD binds to many defaults first."""
    try:
        op.setExpression(prop, None)
    except Exception:  # noqa: BLE001 - no expression bound
        pass
    setattr(op, prop, value)


def _top_faces(shape):
    """Names of the highest upward-facing flat faces (what facing cleans up to)."""
    flat = [(i, f) for i, f in enumerate(shape.Faces, 1)
            if f.Surface.__class__.__name__ == "Plane" and f.normalAt(0, 0).z > 0.999]
    if not flat:
        return []
    z = max(f.BoundBox.ZMax for _, f in flat)
    return [f"Face{i}" for i, f in flat if abs(f.BoundBox.ZMax - z) < 1e-6]


def make_op(o, i, tc, clone, top, floor, report):
    kind = o["type"]
    label = f"{i:02d} {kind}"
    final = floor
    if kind == "face":
        import Path.Op.MillFace as M
        faces = _top_faces(clone.Shape)
        if not faces:
            report["warnings"].append(f"no flat top face to face; {label} skipped")
            return None
        op = M.Create(label)
        op.Base = [(clone, faces)]
        op.BoundaryShape = "Stock"
        op.StepOver = int(round(float(o["stepover"])))
        final = report["model"][5]
    elif kind in ("rough", "finish"):
        import Path.Op.Adaptive as M
        op = M.Create(label)
        # Whole-model, 3D-aware clearing: the only Adaptive mode that needs no face selection.
        op.ModelAwareExperiment = True
        op.StepOver = int(round(float(o["stepover"])))
        op.StockToLeave = f"{o.get('stock_to_leave', 0)} mm"
        op.ZStockToLeave = f"{o.get('stock_to_leave_z', o.get('stock_to_leave', 0))} mm"
        op.FinishingProfile = kind == "finish"
        op.Tolerance = 0.1 if kind == "rough" else 0.05
    elif kind == "profile":
        import Path.Op.Profile as M
        op = M.Create(label)
        op.Side = "Outside"
        op.Direction = "CW" if o.get("climb", True) else "CCW"
        op.UseComp = True
    elif kind == "drill":
        import Path.Op.Drilling as M
        import Path.Base.Drillable as Drillable
        d = float(o["diameter"])
        # Only the holes this drill fits (within 0.05 mm), not every hole it could enter.
        subs = []
        for base, sub in Drillable.getDrillableTargets(clone, ToolDiameter=d, vector=None):
            radii = [e.Curve.Radius for e in base.Shape.getElement(sub).Edges if hasattr(e.Curve, "Radius")]
            if radii and abs(2 * radii[0] - d) <= 0.05:
                subs.append(sub)
        if not subs:
            report["warnings"].append(f"no {d:g} mm holes for drill T{tc.ToolNumber}; {label} skipped")
            return None
        op = M.Create(label)
        op.Base = [(clone, subs)]
        if o.get("peck"):
            op.PeckEnabled = True
            _set(op, "PeckDepth", f"{o['peck']} mm")
        if "ExtraOffset" in op.PropertiesList:
            op.ExtraOffset = "Drill Tip"  # full diameter reaches the bottom of through holes
        report["holes"] = report.get("holes", 0) + len(subs)
    elif kind in ("surface", "waterline"):
        try:
            import ocl  # noqa: F401
        except ImportError:
            try:
                import opencamlib  # noqa: F401
            except ImportError:
                report["warnings"].append(
                    f"{kind} needs OpenCamLib, which this FreeCAD build lacks; {label} skipped")
                return None
        import Path.Op.Surface as S
        import Path.Op.Waterline as W
        op = (S if kind == "surface" else W).Create(label)
        if hasattr(op, "StepOver"):
            op.StepOver = int(round(float(o["stepover"])))
        if kind == "surface":
            op.ScanType = "Planar"
            op.LayerMode = "Single-pass"
    else:
        raise ValueError(f"unknown operation {kind}")
    op.ToolController = tc
    if kind != "drill":
        _set(op, "StartDepth", f"{top} mm")
        _set(op, "FinalDepth", f"{final} mm")
        if hasattr(op, "StepDown"):
            _set(op, "StepDown", f"{o.get('stepdown', max(top - final, 0.1))} mm")
    return op


if __name__ == "__main__" or "PILLOW_CAM_SPEC" in os.environ:
    main()
    if "FreeCAD" in sys.modules:
        try:
            import FreeCAD
            FreeCAD.closeDocument("PillowCAM")
        except Exception:  # noqa: BLE001
            pass
