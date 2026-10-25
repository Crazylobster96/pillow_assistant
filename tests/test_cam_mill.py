"""3-axis milling CAM through FreeCAD: settings, checks, outputs, the cam_mill tool, FreeCAD install.

FreeCAD itself runs only when it is installed (``freecadcmd`` on PATH or
PILLOW_FREECAD); the other tests stand in for it with a fake runner.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pillow_assistant.capabilities import cad_apps, cam_mill
from pillow_assistant.capabilities.cam2d import CamError
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.cam_tool import CamMillTool

MACHINE = {"controller": "grbl", "max_rpm": 24000, "max_feed": 5000, "travel": [300, 200, 80]}
ENDMILL = {"number": 1, "type": "endmill", "diameter": 6, "rpm": 12000, "feed": 900, "plunge": 300,
           "stepdown": 3, "flute_length": 25}
DRILL = {"number": 2, "type": "drill", "diameter": 6, "rpm": 2000, "feed": 150, "plunge": 150}
JOB = {"stock": {"xy": 3, "top": 1, "bottom": 2}, "tools": [ENDMILL, DRILL]}


def test_spec_requires_user_settings_and_respects_machine_limits():
    with pytest.raises(CamError, match="machine.controller.*job.stock"):
        cam_mill.build_spec({}, {"tools": [ENDMILL]})
    with pytest.raises(CamError, match="stepdown is required"):
        cam_mill.build_spec(MACHINE, {**JOB, "tools": [{**ENDMILL, "stepdown": None}]})
    with pytest.raises(CamError, match="max_rpm"):
        cam_mill.build_spec(MACHINE, {**JOB, "tools": [{**ENDMILL, "rpm": 30000}]})
    with pytest.raises(CamError, match="cannot use T2"):
        cam_mill.build_spec(MACHINE, {**JOB, "operations": [{"type": "rough", "tool": 2}]})
    with pytest.raises(CamError, match="percentage"):
        cam_mill.build_spec(MACHINE, {**JOB, "operations": [{"type": "rough", "tool": 1, "stepover": 150}]})
    with pytest.raises(CamError, match="controller must be"):
        cam_mill.build_spec({"controller": "siemens"}, JOB)


def test_default_plan_and_controller_posts():
    spec = cam_mill.build_spec(MACHINE, {**JOB, "tools": JOB["tools"] + [
        {"number": 3, "type": "ballend", "diameter": 3, "rpm": 18000, "feed": 600, "plunge": 200, "stepdown": 1},
        {"number": 4, "type": "endmill", "diameter": 3, "rpm": 18000, "feed": 600, "plunge": 200, "stepdown": 1}]})
    assert [(o["type"], o["tool"]) for o in spec["operations"]] == [
        ("face", 1), ("rough", 1), ("drill", 2), ("finish", 4), ("surface", 3)]
    assert spec["operations"][1]["stock_to_leave"] == 0.3 and spec["operations"][2]["diameter"] == 6
    assert spec["post"] == "grbl" and spec["split_by_tool"]
    no_top = cam_mill.build_spec({"controller": "fanuc"}, {**JOB, "stock": {"xy": 2, "top": 0, "bottom": 0}})
    assert no_top["post"] == "fanuc" and not no_top["split_by_tool"]
    assert [o["type"] for o in no_top["operations"]] == ["rough", "drill", "finish"]


def fake_freecad(min_z=-21.0, rapid_sideways=False):
    """A stand-in for freecadcmd + the runner: writes the report and the program files."""
    def run(cmd, cwd, env, **kw):
        spec = json.loads(Path(env["PILLOW_CAM_SPEC"]).read_text("utf-8"))
        assert cmd[-1].endswith("cam_freecad_runner.py")
        out = Path(spec["output"])
        files = [str(out.with_name(out.stem + "_01_T1.nc"))]
        Path(files[0]).write_text("G21\nM2\n", "utf-8")
        Path(spec["save_project"]).write_text("fcstd", "utf-8")
        moves = [[0, 3, 3, 5], [1, 3, 3, min_z], [1, 83, 3, min_z], [1, 83, 53, min_z]]
        if rapid_sideways:
            moves.append([0, 40, 40, -5])
        report = {"ok": True, "warnings": [], "files": files,
                  "stock": [0, 0, -23, 86, 56, 0], "model": [3, 3, -21, 83, 53, -1],
                  "operations": [{"name": "rough", "label": "_01_rough", "tool": 1, "moves": moves,
                                  "cycle_time": "00:10:30"}]}
        Path(spec["report"]).write_text(json.dumps(report), "utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_mill_writes_programs_preview_sheet_and_project(tmp_path):
    src = tmp_path / "part.step"
    src.write_text("ISO-10303-21;", "utf-8")
    result = cam_mill.mill(src, tmp_path / "part.nc", MACHINE, JOB, ["freecadcmd"],
                           runner=fake_freecad(rapid_sideways=True))
    assert [Path(p).name for p in result["programs"]] == ["part_01_T1.nc"]
    assert Path(result["preview"]).read_text("utf-8").startswith("<svg")
    sheet = Path(result["sheet"]).read_text("utf-8")
    assert "86 x 56 x 23 mm" in sheet and "| 1 | 6mm endmill | 6 | 12000 | 900 | 300 | 3 |" in sheet
    assert result["minutes"] == pytest.approx(10.5)
    assert any("sideways below the stock top" in w for w in result["warnings"])


def test_mill_refuses_cuts_below_stock_and_beyond_travel(tmp_path):
    src = tmp_path / "part.step"
    src.write_text("ISO-10303-21;", "utf-8")
    with pytest.raises(CamError, match="below the bottom of the stock"):
        cam_mill.mill(src, tmp_path / "a.nc", MACHINE, JOB, ["freecadcmd"], runner=fake_freecad(min_z=-25))
    assert not (tmp_path / "a_01_T1.nc").exists()  # nothing unsafe is left behind
    with pytest.raises(CamError, match="Z travel"):
        cam_mill.mill(src, tmp_path / "b.nc", {**MACHINE, "travel": [300, 200, 20]}, JOB, ["freecadcmd"],
                      runner=fake_freecad())
    short = {**JOB, "tools": [{**ENDMILL, "flute_length": 12}, DRILL]}
    result = cam_mill.mill(src, tmp_path / "c.nc", MACHINE, short, ["freecadcmd"], runner=fake_freecad())
    assert any("cuts 21 mm deep but its flutes are 12 mm" in w for w in result["warnings"])


def test_freecad_failure_is_reported(tmp_path):
    def broken(cmd, cwd, env, **kw):
        spec = json.loads(Path(env["PILLOW_CAM_SPEC"]).read_text("utf-8"))
        Path(spec["report"]).write_text(json.dumps({"ok": False, "error": "ValueError: no solid"}), "utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    src = tmp_path / "part.step"
    src.write_text("x", "utf-8")
    with pytest.raises(CamError, match="no solid"):
        cam_mill.mill(src, tmp_path / "part.nc", MACHINE, JOB, ["freecadcmd"], runner=broken)


def test_cam_mill_tool_runs_freecad_from_cad_apps(monkeypatch, tmp_path):
    (tmp_path / "part.step").write_text("ISO-10303-21;", "utf-8")
    monkeypatch.setattr(cad_apps, "locate", lambda key: Path("/opt/freecad/bin/freecadcmd"))
    monkeypatch.setattr(cam_mill.subprocess, "run", fake_freecad())
    tool, ctx = CamMillTool(), ToolContext(workspace=tmp_path)
    result = asyncio.run(tool({"input": "part.step", "machine": MACHINE, "job": JOB}, ctx))
    assert result.ok, result.text
    assert result.artifacts == ["part_01_T1.nc", "part.preview.svg", "part.setup.md", "part.FCStd"]
    bad = asyncio.run(tool({"input": "part.step", "machine": MACHINE, "job": {"stock": JOB["stock"]}}, ctx))
    assert not bad.ok and "job.tools" in bad.text
    assert not asyncio.run(tool({"input": "../part.step", "machine": MACHINE, "job": JOB}, ctx)).ok


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fake AppImage is a shell script")
def test_freecad_appimage_is_extracted_on_install(monkeypatch, tmp_path):
    monkeypatch.setenv("PILLOW_CAD_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.delenv("PILLOW_FREECAD", raising=False)
    monkeypatch.setattr(cad_apps.shutil, "which", lambda name: None)
    monkeypatch.setattr(cad_apps.sys, "platform", "linux")
    monkeypatch.setattr(cad_apps, "platform_key", lambda: "linux-x64")
    asset = "FreeCAD_1.1.3-conda-Linux-x86_64-py311.AppImage"
    releases = [{"tag_name": "1.1.3", "prerelease": False,
                 "assets": [{"name": asset + "-SHA256.txt", "browser_download_url": "s"},
                            {"name": asset, "browser_download_url": "https://example.invalid/" + asset}]}]

    def download(url, dest):
        assert url.endswith(asset)
        dest.write_text("#!/bin/sh\nmkdir -p squashfs-root/usr/bin && touch squashfs-root/usr/bin/freecadcmd\n")

    result = cad_apps.install("freecad", get_json=lambda url: releases, download=download)
    assert result["path"].endswith("squashfs-root/usr/bin/freecadcmd") and result["version"] == "1.1.3"
    assert not list((tmp_path / "apps" / "freecad").glob("*.AppImage"))


def _freecad():
    path = os.environ.get("PILLOW_FREECAD") or cad_apps.shutil.which("freecadcmd")
    return [path] if path else None


@pytest.mark.skipif(_freecad() is None, reason="FreeCAD (freecadcmd) is not installed")
def test_real_freecad_mills_a_plate_with_a_pocket_and_holes(tmp_path):
    script = tmp_path / "make.py"
    script.write_text(
        "import FreeCAD, Part\n"
        "s = Part.makeBox(60, 40, 15)\n"
        "s = s.cut(Part.makeBox(30, 16, 6, FreeCAD.Vector(15, 12, 9)))\n"
        "for x in (6, 54):\n"
        "    s = s.cut(Part.makeCylinder(3, 30, FreeCAD.Vector(x, 20, -5)))\n"
        f"s.exportStep({str(tmp_path / 'plate.step')!r})\n", "utf-8")
    subprocess.run(_freecad() + [str(script)], capture_output=True, timeout=300)
    result = cam_mill.mill(tmp_path / "plate.step", tmp_path / "plate.nc", MACHINE, JOB, _freecad())
    names = [Path(p).name for p in result["programs"]]
    assert names == ["plate_01_T1.nc", "plate_02_T2.nc", "plate_03_T1.nc"]
    drill = Path(result["programs"][1]).read_text("utf-8")
    assert "M3 S2000" in drill and drill.count("G0 X9.000 Y23.000") + drill.count("G0 X57.000 Y23.000") >= 2
    assert Path(result["project"]).stat().st_size > 1000
