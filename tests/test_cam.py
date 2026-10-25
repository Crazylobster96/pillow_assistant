"""text-to-cam: 2D laser / plasma toolpaths from DXF, and the text_to_cam tool."""

import asyncio
import json
import math

import pytest

from pillow_assistant.capabilities import cam2d
from pillow_assistant.capabilities.skill_registry import CapabilitySkillRegistry
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin import build_default_registry
from pillow_assistant.core.tools.builtin.cam_tool import TextToCamTool

LASER = {"controller": "grbl", "process": "laser", "max_power": 1000}
LASER_JOB = {"kerf": 0.2, "feed": 600, "power": 800}


def dxf(*entities: list, insunits: int = 4) -> str:
    """A minimal ASCII DXF from (code, value) lists, one list per entity."""
    pairs = [(0, "SECTION"), (2, "HEADER"), (9, "$INSUNITS"), (70, insunits), (0, "ENDSEC"),
             (0, "SECTION"), (2, "ENTITIES")]
    for e in entities:
        pairs += e
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    return "\n".join(f"{c}\n{v}" for c, v in pairs) + "\n"


def rect(x0, y0, x1, y1, layer="0"):
    return [(0, "LWPOLYLINE"), (8, layer), (90, 4), (70, 1),
            (10, x0), (20, y0), (10, x1), (20, y0), (10, x1), (20, y1), (10, x0), (20, y1)]


def circle(cx, cy, r, layer="0"):
    return [(0, "CIRCLE"), (8, layer), (10, cx), (20, cy), (30, 0), (40, r)]


def line(x0, y0, x1, y1, layer="0"):
    return [(0, "LINE"), (8, layer), (10, x0), (20, y0), (11, x1), (21, y1)]


def arc(cx, cy, r, a0, a1):
    return [(0, "ARC"), (8, "0"), (10, cx), (20, cy), (40, r), (50, a0), (51, a1)]


def extents(path):
    xs, ys = [p[0] for p in path], [p[1] for p in path]
    return min(xs), max(xs), min(ys), max(ys)


def test_kerf_offset_orientation_and_inside_out_order():
    shapes, scale, warnings = cam2d.read_dxf(dxf(rect(0, 0, 100, 60), circle(20, 30, 5)))
    assert scale == 1.0 and not warnings
    plan = cam2d.plan(shapes, scale, cam2d.settings(LASER, LASER_JOB))
    hole, outer = plan.cuts
    assert (hole.kind, outer.kind) == ("hole", "outer")  # holes first, the part drops last
    assert extents(outer.path) == pytest.approx((-0.1, 100.1, -0.1, 60.1), abs=1e-6)
    x0, x1, y0, y1 = extents(hole.path)
    assert (x1 - x0) / 2 == pytest.approx(4.9, abs=0.02)
    # Part on the right of travel: outer clockwise, holes counter-clockwise.
    assert cam2d.area(outer.path[:-1]) < 0 < cam2d.area(hole.path[:-1])
    assert outer.path[0] == outer.path[-1]


def test_lwpolyline_bulges_units_and_chained_pieces():
    b = math.tan(math.radians(180) / 4)  # two half-circle bulges make a circle of radius 5
    disc = [(0, "LWPOLYLINE"), (8, "0"), (90, 2), (70, 1), (10, -5), (20, 0), (42, b), (10, 5), (20, 0), (42, b)]
    slot = [line(0, 20, 10, 20), arc(10, 25, 5, -90, 90), line(10, 30, 0, 30), arc(0, 25, 5, 90, 270)]
    shapes, scale, _ = cam2d.read_dxf(dxf(disc, *slot, insunits=1))
    assert scale == 25.4
    loops, opens = cam2d.chain(shapes, 0.05)
    assert len(loops) == 2 and not opens
    pts = loops[0].points if len(loops[0].points) > 10 and abs(loops[0].points[0][1]) < 6 else loops[1].points
    assert all(math.hypot(x, y) == pytest.approx(5, abs=0.02) for x, y in pts)


def test_nested_island_and_lead_in_stays_inside_hole():
    shapes, scale, _ = cam2d.read_dxf(dxf(rect(0, 0, 100, 100), rect(20, 20, 80, 80), circle(50, 50, 10)))
    plan = cam2d.plan(shapes, scale, cam2d.settings(LASER, {**LASER_JOB, "lead_in": 3}))
    assert [c.kind for c in plan.cuts] == ["outer", "hole", "outer"]  # island, window, sheet
    window = plan.cuts[1]
    assert window.lead and cam2d.inside(window.lead[0], window.path[:-1])
    sheet = plan.cuts[2]
    assert not cam2d.inside(sheet.lead[0], sheet.path[:-1])


def test_hole_smaller_than_kerf_is_skipped_with_warning():
    shapes, scale, _ = cam2d.read_dxf(dxf(rect(0, 0, 50, 50), circle(25, 25, 0.5)))
    plan = cam2d.plan(shapes, scale, cam2d.settings(LASER, {**LASER_JOB, "kerf": 1.5}))
    assert [c.kind for c in plan.cuts] == ["outer"]
    assert any("smaller than the kerf" in w for w in plan.warnings)


def test_settings_never_guess_safety_values():
    with pytest.raises(cam2d.CamError, match="feed, power"):
        cam2d.settings(LASER, {"kerf": 0.1})
    with pytest.raises(cam2d.CamError, match="pierce_delay"):
        cam2d.settings({"controller": "grbl", "process": "plasma"}, {"kerf": 1, "feed": 2000})
    with pytest.raises(cam2d.CamError, match="max_power"):
        cam2d.settings(LASER, {**LASER_JOB, "power": 5000})
    with pytest.raises(cam2d.CamError, match="single short"):
        cam2d.settings(LASER, {**LASER_JOB, "torch_on": "M3\nG0 Z-50"})


def test_grbl_laser_program():
    program, svg, st, _ = cam2d.cut2d(dxf(rect(0, 0, 10, 10)), LASER, {**LASER_JOB, "origin": "lower_left"})
    lines = program.splitlines()
    assert "G21 G90 G94" in lines and "M4 S800" in lines and lines[-1] == "M2"
    assert any(l.startswith("G1 ") and l.endswith(" F600") for l in lines)
    assert all(not l.startswith("G1 X-") for l in lines)  # lower_left keeps the path in positive space
    assert st["pierces"] == 1 and st["cut_mm"] == pytest.approx(40 + 2 * math.pi * 0.1, abs=0.05)
    assert svg.startswith("<svg")


def test_fanuc_plasma_program_with_z_heights():
    machine = {"controller": "fanuc", "process": "plasma", "z_axis": True, "safe_height": 10,
               "pierce_height": 3.8, "cut_height": 1.5}
    program, _, _, _ = cam2d.cut2d(dxf(rect(0, 0, 50, 50)), machine,
                                   {"kerf": 1.5, "feed": 2500, "pierce_delay": 0.5, "lead_in": 4})
    lines = program.splitlines()
    assert lines[:2] == ["%", "O0001"] and lines[-2:] == ["M30", "%"]
    i = lines.index("G0 Z3.8")
    assert lines[i + 1:i + 4] == ["M3", "G4 X0.5", "G1 Z1.5 F2500"]
    assert all(l == l.upper() for l in lines)


def test_bed_check_refuses_out_of_range_paths():
    with pytest.raises(cam2d.CamError, match="does not fit"):
        cam2d.cut2d(dxf(rect(0, 0, 500, 10)), {**LASER, "bed": [300, 200]}, {**LASER_JOB, "origin": "lower_left"})


def test_engrave_layer_is_cut_first_on_the_line():
    program_dxf = dxf(rect(0, 0, 40, 40), line(5, 20, 35, 20, layer="ENGRAVE"))
    plan = cam2d.plan(*cam2d.read_dxf(program_dxf)[:2],
                      cam2d.settings(LASER, {**LASER_JOB, "engrave_layers": ["ENGRAVE"], "engrave_power": 200}))
    assert [c.kind for c in plan.cuts] == ["engrave", "outer"]
    assert {p[1] for p in plan.cuts[0].path} == {20.0}
    assert "M4 S200" in cam2d.gcode(plan)


def test_tool_writes_program_and_preview(tmp_path):
    (tmp_path / "part.dxf").write_text(dxf(rect(0, 0, 30, 20), circle(15, 10, 4)), "utf-8")
    (tmp_path / "cam" / "machines").mkdir(parents=True)
    (tmp_path / "cam" / "machines" / "shop.json").write_text(json.dumps({**LASER, "bed": [400, 300]}), "utf-8")
    tool = TextToCamTool()
    result = asyncio.run(tool({"action": "cut2d", "input": "part.dxf", "machine_file": "cam/machines/shop.json",
                               "job": {**LASER_JOB, "origin": "lower_left"}}, ToolContext(workspace=tmp_path)))
    assert result.ok, result.text
    assert result.artifacts == ["part.nc", "part.preview.svg"]
    assert (tmp_path / "part.nc").read_text("utf-8").count("M4 S800") == 2


def test_tool_refuses_outside_workspace_and_missing_settings(tmp_path):
    (tmp_path / "part.dxf").write_text(dxf(rect(0, 0, 30, 20)), "utf-8")
    tool, ctx = TextToCamTool(), ToolContext(workspace=tmp_path)
    assert not asyncio.run(tool({"input": "../x.dxf", "machine": LASER, "job": LASER_JOB}, ctx)).ok
    assert not asyncio.run(tool({"input": "part.dxf", "output": "../x.nc", "machine": LASER, "job": LASER_JOB},
                                ctx)).ok
    result = asyncio.run(tool({"input": "part.dxf", "machine": LASER, "job": {"kerf": 0.2}}, ctx))
    assert not result.ok and "feed" in result.text
    assert not (tmp_path / "part.nc").exists()


def test_tool_and_skill_are_registered(tmp_path):
    assert "text_to_cam" in build_default_registry().names()
    skills = {s.name: s for s in CapabilitySkillRegistry(user_root=tmp_path / "none").load()}
    assert skills["cam"].source_kind == "builtin"
    assert "text_to_cam" in skills["cam"].tools
