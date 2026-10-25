"""3+2 / 4th-axis indexed milling: rotary angles, setup checks, rotary blocks in the programs.

FreeCAD itself runs only when it is installed (see test_cam_mill); the other
tests stand in for it with a fake runner.
"""

import asyncio
import json
import math
import subprocess
from pathlib import Path

import pytest

from pillow_assistant.capabilities import cad_apps, cam_indexed as ci, cam_mill
from pillow_assistant.capabilities.cam2d import CamError
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.cam_tool import CamMillTool
from tests.test_cam_mill import ENDMILL, MACHINE, _freecad

ROTARY = {**MACHINE, "controller": "linuxcnc", "rotary": {"axes": "A", "limits": {"A": [-360, 360]}}}
JOB = {"stock": {"xy": 3, "top": 1, "bottom": 1}, "tools": [ENDMILL], "hold": 10,
       "setups": [{"direction": "+Z"}, {"direction": "+Y"}, {"direction": "-Z"}, {"direction": "-Y"}]}


@pytest.mark.parametrize("axes, directions", [
    ("A", ["+Z", "+Y", "-Z", "-Y", [0, 1, 1]]),
    ("B", ["+Z", "+X", "-Z", "-X", [1, 0, 2]]),
    ("AC", ["+Z", "+X", "-Y", [1, 1, 1], [-1, 2, -0.5]]),
    ("BC", ["+Z", "-X", "+Y", [1, 1, 1], [-1, 2, -0.5]]),
])
def test_angles_turn_the_tool_direction_to_plus_z(axes, directions):
    for d in directions:
        v = ci.direction(d)
        r = ci.rotation(axes, ci.angles_for(axes, v))
        assert ci.apply(r, v) == pytest.approx((0, 0, 1), abs=1e-9)
    assert ci.angles_for("A", ci.direction("+Y")) == {"A": pytest.approx(90)}
    with pytest.raises(CamError, match="no X component"):
        ci.angles_for("A", (1, 0, 0))
    with pytest.raises(CamError, match="no Y component"):
        ci.angles_for("B", (0, 1, 0))


def test_tilted_plane_euler_angles_rebuild_the_frame():
    for angles in ({"A": 90, "C": 90}, {"A": 30, "C": 45}, {"A": -60, "C": 200}, {"A": 0, "C": 30}):
        r = ci.rotation("AC", angles)
        i, j, k = ci.euler_zxz(r)
        m = ci.mul(ci.mul(ci.rz(i), ci.rx(j)), ci.rz(k))
        assert sum(m, []) == pytest.approx([r[b][a] for a in range(3) for b in range(3)], abs=1e-9)
    assert ci.euler_zxz(ci.rotation("AC", {"A": 90})) == pytest.approx((0, -90, 0))


def test_setups_are_checked_before_anything_runs():
    with pytest.raises(CamError, match="machine.rotary"):
        ci.plan_setups(MACHINE, JOB)
    with pytest.raises(CamError, match="outside the A axis limits"):
        ci.plan_setups({**ROTARY, "rotary": {"axes": "A", "limits": {"A": [-30, 120]}}}, JOB)
    with pytest.raises(CamError, match="no X component"):
        ci.plan_setups(ROTARY, {**JOB, "setups": [{"direction": "+X"}]})
    with pytest.raises(CamError, match="Fanuc G68.2"):
        ci.plan_setups({**ROTARY, "rotary": {"axes": "AC", "mode": "tilted_plane"}}, JOB)
    with pytest.raises(CamError, match="no C axis"):
        ci.plan_setups(ROTARY, {**JOB, "setups": [{"A": 90, "C": 10}]})
    with pytest.raises(CamError, match="single short G-code line"):
        ci.plan_setups({**ROTARY, "rotary": {"axes": "A", "retract": "G0 Z50\nM30"}}, JOB)
    with pytest.raises(CamError, match="setup 2 operation 1"):
        ci.plan_setups(ROTARY, {**JOB, "setups": [{"A": 0}, {"A": 90, "operations": [{"type": "drill", "tool": 1}]}]})
    rot, setups = ci.plan_setups({**ROTARY, "rotary": {"axes": "A", "signs": {"A": -1}}}, JOB)
    assert [s["angles"] for s in setups] == [{"A": 0}, {"A": -90}, {"A": -180}, {"A": 90}]
    assert all(s["floor"] == 0 for s in setups) and rot["retract"] == "G53 G0 Z0"


def test_rotary_blocks_turn_before_the_tool_change_or_tilt_after_it():
    _, setups = ci.plan_setups(ROTARY, JOB)
    table = ci.add_rotary_blocks("G17 G90\nG21\nM6 T1\nG43 H1\nM3 S12000\nG0 X1\nM2\n", "linuxcnc",
                                 {"mode": "table", "retract": "G53 G0 Z0"}, setups[1], 4)
    assert table.splitlines()[2:6] == ["(SETUP 2/4 S2: A90.000)", "G53 G0 Z0", "G90 G0 A90.000", "M6 T1"]
    rot = {"mode": "tilted_plane", "retract": "G28 G91 Z0"}
    setup = {"index": 1, "name": "side", "rotation": ci.rotation("AC", {"A": 90}), "angles": {}}
    tilted = ci.add_rotary_blocks("G21\nM6 T1\nG43 H1 Z50\nG0 X1\nM6 T2\nG43 H2 Z50\nG0 X2\nM30\n", "fanuc",
                                  rot, setup, 1).splitlines()
    assert tilted == ["G21", "G69", "M6 T1", "G43 H1 Z50", "(SETUP 1/1 SIDE: TILTED PLANE)", "G28 G91 Z0", "G90",
                      "G68.2 X0 Y0 Z0 I0.000 J-90.000 K0.000", "G53.1", "G0 X1", "G69", "M6 T2", "G43 H2 Z50",
                      "(SETUP 1/1 SIDE: TILTED PLANE)", "G28 G91 Z0", "G90",
                      "G68.2 X0 Y0 Z0 I0.000 J-90.000 K0.000", "G53.1", "G0 X2", "G69", "M30"]


def fake_indexed():
    """A stand-in for FreeCAD: one program per setup, the stock centred on the A axis."""
    seen = []

    def run(cmd, cwd, env, **kw):
        spec = json.loads(Path(env["PILLOW_CAM_SPEC"]).read_text("utf-8"))
        seen.append(spec)
        assert spec["indexed"]["mount"] == "A" and spec["indexed"]["floor"] == 0
        out = Path(spec["output"])
        out.write_text("G17 G54 G90\nG21\nM6 T1\nG43 H1\nM3 S12000\nG0 X10 Y0 Z15\nG1 Z0 F300\nM2\n", "utf-8")
        Path(spec["save_project"]).write_text("fcstd", "utf-8")
        moves = [[0, 10, 0, 15], [1, 10, 0, 0], [1, 60, 0, 0]]
        report = {"ok": True, "warnings": [], "files": [str(out)], "stock": [-10, -23, -10.5, 66, 23, 10.5],
                  "model": [3, -20, -7.5, 63, 20, 7.5],
                  "operations": [{"name": "rough", "label": "_01_rough", "tool": 1, "moves": moves,
                                  "cycle_time": "00:02:00"}]}
        Path(spec["report"]).write_text(json.dumps(report), "utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    run.seen = seen
    return run


def test_mill_indexed_writes_one_program_per_setup(tmp_path):
    src = tmp_path / "part.step"
    src.write_text("ISO-10303-21;", "utf-8")
    runner = fake_indexed()
    result = cam_mill.mill(src, tmp_path / "part.nc", ROTARY, JOB, ["freecadcmd"], runner=runner)
    assert [Path(p).name for p in result["programs"]] == [f"part_S{i}.nc" for i in range(1, 5)]
    assert len(result["previews"]) == 4 and len(result["projects"]) == 4
    assert sum(runner.seen[1]["indexed"]["rotation"], []) == pytest.approx(sum(ci.rx(90), []))
    assert runner.seen[0]["indexed"]["hold"] == 10
    assert "G90 G0 A-90.000" in Path(result["programs"][3]).read_text("utf-8")
    assert result["minutes"] == pytest.approx(8)
    assert any("reaches 25 mm from the rotary centre" in w for w in result["warnings"])
    sheet = Path(result["sheet"]).read_text("utf-8")
    assert "## Setup 2: S2 (A90.000, tool from [0, 1, 0])" in sheet and "10 mm stub" in sheet


def test_cam_mill_tool_lists_every_setup(monkeypatch, tmp_path):
    (tmp_path / "part.step").write_text("ISO-10303-21;", "utf-8")
    monkeypatch.setattr(cad_apps, "locate", lambda key: Path("/opt/freecad/bin/freecadcmd"))
    monkeypatch.setattr(cam_mill.subprocess, "run", fake_indexed())
    tool, ctx = CamMillTool(), ToolContext(workspace=tmp_path)
    job = {**JOB, "setups": JOB["setups"][:2]}
    result = asyncio.run(tool({"input": "part.step", "machine": ROTARY, "job": job}, ctx))
    assert result.ok, result.text
    assert result.artifacts == ["part_S1.nc", "part_S2.nc", "part_S1.preview.svg", "part_S2.preview.svg",
                                "part.setup.md", "part_S1.FCStd", "part_S2.FCStd"]
    bad = asyncio.run(tool({"input": "part.step", "machine": MACHINE, "job": job}, ctx))
    assert not bad.ok and "machine.rotary" in bad.text


@pytest.mark.skipif(_freecad() is None, reason="FreeCAD (freecadcmd) is not installed")
def test_real_freecad_machines_four_sides_on_an_a_axis(tmp_path):
    script = tmp_path / "make.py"
    script.write_text(
        "import FreeCAD, Part\n"
        "s = Part.makeBox(60, 30, 30)\n"
        "s = s.cut(Part.makeBox(20, 31, 8, FreeCAD.Vector(20, -0.5, 22)))\n"
        "s = s.cut(Part.makeBox(20, 8, 31, FreeCAD.Vector(20, 22, -0.5)))\n"
        f"s.exportStep({str(tmp_path / 'bar.step')!r})\n", "utf-8")
    subprocess.run(_freecad() + [str(script)], capture_output=True, timeout=300)
    job = {"stock": {"xy": 2, "top": 0, "bottom": 0}, "tools": [ENDMILL], "hold": 10,
           "setups": [{"direction": "+Z", "operations": [{"type": "rough", "tool": 1}]},
                      {"direction": "+Y", "operations": [{"type": "rough", "tool": 1}]}]}
    result = cam_mill.mill(tmp_path / "bar.step", tmp_path / "bar.nc", ROTARY, job, _freecad())
    for path, angle in zip(result["programs"], ("A0.000", "A90.000")):
        text = Path(path).read_text("utf-8")
        assert f"G90 G0 {angle}" in text
        xs, zs = [], []
        for line in text.splitlines():
            if line.startswith(("G0", "G1", "G2", "G3")):
                for axis, value in ((w[0], w[1:]) for w in line.split() if w[0] in "XZ"):
                    (xs if axis == "X" else zs).append(float(value))
        assert min(xs) >= -1e-6, "nothing may be cut in the chuck stub (X < 0)"
        assert 0 - 1e-6 <= min(zs) < 15, "cuts reach into the part but not past the rotary axis"
    assert math.isfinite(result["minutes"]) and result["minutes"] > 0
