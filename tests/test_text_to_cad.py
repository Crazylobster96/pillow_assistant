"""text-to-cad integration: vendored Skills, uv command line, and the two CAD tools.

The real cadgen runtime is only exercised when uv is installed and
PILLOW_TEST_CADGEN=1 (it downloads ~1 GB on first use).
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from pillow_assistant.capabilities import text_to_cad
from pillow_assistant.capabilities.skill_registry import CapabilitySkillRegistry
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.cad_tool import CadReferenceTool, TextToCadTool
from pillow_assistant.core.tools.builtin.skill_tool import SkillTool

ROOT = Path(__file__).resolve().parents[1]


def test_vendored_skills_and_upstream_pin():
    info = text_to_cad.upstream_info()
    assert info["repo"] == "earthtojake/text-to-cad"
    assert info["version"] and info["cadgen_version"]
    assert info["uvx_args"][-2:] == ["--from", f"cadgen=={info['cadgen_version']}"]
    root = text_to_cad.skills_root()
    assert (root / "cad" / "SKILL.md").is_file()
    assert not list(root.glob("*/agents"))  # per-agent UI metadata is not vendored


def test_registry_layers_text_to_cad_between_builtin_and_user(tmp_path):
    user = tmp_path / "user"
    (user / "cad").mkdir(parents=True)
    (user / "cad" / "SKILL.md").write_text("---\nname: cad\ndescription: mine\n---\nbody", "utf-8")
    skills = {s.name: s for s in CapabilitySkillRegistry(user_root=user).load()}
    assert skills["cad"].source_kind == "user"  # users can still override
    assert skills["dxf"].source_kind == text_to_cad.SOURCE_KIND
    assert skills["dxf"].description


def test_apply_skill_prepends_pillow_adapter_note(tmp_path):
    skills = CapabilitySkillRegistry(user_root=tmp_path / "none").load()
    tool = SkillTool(skills)
    result = asyncio.run(tool({"name": "cad"}, ToolContext(workspace=tmp_path)))
    assert result.ok
    assert "text_to_cad" in result.text and "cad_reference" in result.text
    assert "# CAD modeling and inspection" in result.text


def test_cadgen_argv_pins_runtime(monkeypatch):
    monkeypatch.setattr(text_to_cad, "find_uv", lambda: ["uvx"])
    info = {"uvx_args": ["--no-config", "--managed-python", "--python", "3.13", "--from", "cadgen==1.2.3"]}
    argv = text_to_cad.cadgen_argv("python", ["src/a.py"], info=info, with_requirements=Path("req.txt"),
                                   offline=True)
    assert argv == ["uvx", "--offline", "--no-config", "--managed-python", "--python", "3.13",
                    "--with-requirements", "req.txt", "--from", "cadgen==1.2.3", "python", "src/a.py"]
    monkeypatch.setattr(text_to_cad, "find_uv", lambda: None)
    assert text_to_cad.cadgen_argv("cadgen", [], info=info) is None


def test_needs_download_only_for_cache_misses():
    miss = "error: No interpreter found for Python 3.13\nhint: ... but uv is set to offline mode"
    assert text_to_cad.needs_download(2, miss)
    assert not text_to_cad.needs_download(0, miss)
    assert not text_to_cad.needs_download(1, "Traceback: ValueError: bad fillet")


def test_text_to_cad_tool_falls_back_to_download(monkeypatch, tmp_path):
    import subprocess

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--offline" in cmd:
            return subprocess.CompletedProcess(cmd, 2, "", "uv is set to offline mode")
        (tmp_path / "STEP").mkdir(exist_ok=True)
        (tmp_path / "STEP" / "a.step").write_text("ISO-10303-21;", "utf-8")
        return subprocess.CompletedProcess(cmd, 0, "[cadgen] wrote STEP", "")

    monkeypatch.setattr(text_to_cad, "find_uv", lambda: ["uvx"])
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = asyncio.run(TextToCadTool()({"program": "python", "args": ["src/a.py"]},
                                         ToolContext(workspace=tmp_path)))
    assert result.ok and result.artifacts == ["STEP/a.step"]
    assert len(calls) == 2 and "--offline" not in calls[1]
    assert calls[1][-2:] == ["python", "src/a.py"]


def test_text_to_cad_tool_resolves_skill_scripts(monkeypatch, tmp_path):
    import subprocess

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(text_to_cad, "find_uv", lambda: ["uvx"])
    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = ToolContext(workspace=tmp_path)
    result = asyncio.run(TextToCadTool()(
        {"program": "python", "args": ["scripts/mold_tool.py", "measure", "part.stl"], "skill": "dfm"}, ctx))
    assert result.ok
    cmd = seen["cmd"]
    assert cmd[cmd.index("--with-requirements") + 1].endswith(os.path.join("dfm", "requirements.txt"))
    assert cmd[-3].endswith(os.path.join("dfm", "scripts", "mold_tool.py"))
    assert seen["env"]["DO_NOT_TRACK"] == "1"

    bad = asyncio.run(TextToCadTool()({"program": "bash", "args": []}, ctx))
    assert not bad.ok
    bad = asyncio.run(TextToCadTool()({"program": "cadgen", "args": [], "skill": "../x"}, ctx))
    assert not bad.ok
    off = asyncio.run(TextToCadTool()({"program": "cadgen", "args": ["--version"]},
                                      ToolContext(workspace=tmp_path, allow_cli=False)))
    assert not off.ok


def test_cad_reference_reads_only_inside_skills(tmp_path):
    tool = CadReferenceTool()
    ctx = ToolContext(workspace=tmp_path)
    status = asyncio.run(tool({}, ctx))
    assert status.ok and "cad:" in status.text
    ref = asyncio.run(tool({"path": "cad/references/step-generation.md"}, ctx))
    assert ref.ok and ref.text.strip()
    listing = asyncio.run(tool({"path": "cad"}, ctx))
    assert "cad/SKILL.md" in listing.text
    assert not asyncio.run(tool({"path": "../../tools/run_cli/manifest.json"}, ctx)).ok
    assert not asyncio.run(tool({"path": "cad/missing.md"}, ctx)).ok


def test_sync_script_vendors_a_checkout(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("sync_text_to_cad", ROOT / "scripts" / "sync_text_to_cad.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    src = tmp_path / "upstream"
    (src / "skills" / "cad" / "agents").mkdir(parents=True)
    (src / "skills" / "cad" / "SKILL.md").write_text("---\nname: cad\n---\nx", "utf-8")
    (src / "skills" / "cad" / "agents" / "openai.yaml").write_text("x", "utf-8")
    (src / "LICENSE").write_text("MIT", "utf-8")
    (src / "plugin.json").write_text(json.dumps({"version": "9.9.9"}), "utf-8")
    (src / "mcp.json").write_text(json.dumps({"mcpServers": {"cad": {"args": [
        "--no-config", "--python", "3.13", "--from", "cadgen==9.9.8", "cadgen", "mcp"]}}}), "utf-8")
    dest = tmp_path / "out" / "text_to_cad"
    monkeypatch.setattr(mod, "DEST", dest)
    meta = mod.read_upstream(src)
    assert meta["cadgen_version"] == "9.9.8"
    assert meta["uvx_args"] == ["--no-config", "--python", "3.13", "--from", "cadgen==9.9.8"]
    mod.vendor(src, dest, meta, "latest")
    assert (dest / "cad" / "SKILL.md").is_file() and not (dest / "cad" / "agents").exists()
    assert json.loads((dest / "UPSTREAM.json").read_text("utf-8"))["version"] == "9.9.9"


@pytest.mark.skipif(os.environ.get("PILLOW_TEST_CADGEN") != "1" or text_to_cad.find_uv() is None,
                    reason="set PILLOW_TEST_CADGEN=1 with uv installed to run the real cadgen runtime")
def test_real_cadgen_builds_step(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "plate.py").write_text(
        "from cadgen import build123d as bd\nfrom cadgen import step\n\n"
        "@step(out='../STEP/plate.step')\ndef plate():\n    b = bd.Box(30, 20, 4)\n    b.label = 'plate'\n    return b\n\n"
        "if __name__ == '__main__':\n    plate()\n", "utf-8")
    result = asyncio.run(TextToCadTool()({"program": "python", "args": ["src/plate.py"]},
                                         ToolContext(workspace=tmp_path)))
    assert result.ok, result.text
    assert "STEP/plate.step" in result.artifacts


def _fake_releases(name):
    return [{"tag_name": "v9.0.0-beta", "prerelease": True, "assets": [{"name": name, "browser_download_url": "x"}]},
            {"tag_name": "v8.1.0", "prerelease": False,
             "assets": [{"name": "notes.txt", "browser_download_url": "n"},
                        {"name": name, "browser_download_url": "https://example.invalid/" + name}]}]


def test_cad_app_installs_portable_orcaslicer_on_first_use(monkeypatch, tmp_path):
    import io
    import zipfile

    from pillow_assistant.capabilities import cad_apps

    monkeypatch.setenv("PILLOW_CAD_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(cad_apps.shutil, "which", lambda name: None)
    monkeypatch.setattr(cad_apps.sys, "platform", "win32")
    monkeypatch.setattr(cad_apps, "platform_key", lambda: "windows-x64")
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "PILLOW_ORCASLICER"):
        monkeypatch.delenv(var, raising=False)
    asset = "OrcaSlicer_Windows_V8.1.0_x64_portable.zip"
    assert cad_apps.locate("orcaslicer") is None

    def download(url, dest):
        assert url.endswith(asset)  # skips the pre-release
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("OrcaSlicer/orca-slicer.exe", "exe")
            zf.writestr("OrcaSlicer/resources/profiles/BBL.json", "{}")
        dest.write_bytes(buf.getvalue())

    result = cad_apps.install("orcaslicer", get_json=lambda url: _fake_releases(asset), download=download)
    assert result["version"] == "v8.1.0" and result["path"].endswith("orca-slicer.exe")
    app = cad_apps.locate("orcaslicer")
    assert app is not None and cad_apps.installed_version("orcaslicer") == "v8.1.0"
    assert cad_apps.profiles_dir("orcaslicer", app).name == "profiles"


def test_cad_app_rejects_unsafe_archives(monkeypatch, tmp_path):
    import io
    import zipfile

    from pillow_assistant.capabilities import cad_apps

    monkeypatch.setenv("PILLOW_CAD_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(cad_apps, "platform_key", lambda: "windows-x64")
    asset = "Bambu_Studio_win_v8.zip"

    def download(url, dest):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.exe", "x")
        dest.write_bytes(buf.getvalue())

    with pytest.raises(cad_apps.CadAppError):
        cad_apps.install("bambu-studio", get_json=lambda url: _fake_releases(asset), download=download)
    assert not (tmp_path / "evil.exe").exists()


def test_bambu_connect_opens_official_page(monkeypatch):
    from pillow_assistant.capabilities import cad_apps

    opened = []
    assert cad_apps.install("bambu-connect", open_page=opened.append) == {"opened": cad_apps.APPS["bambu-connect"].download_page}
    assert opened and opened[0].startswith("https://bambulab.com/")


def test_cad_app_tool_run_and_open(monkeypatch, tmp_path):
    import subprocess

    from pillow_assistant.capabilities import cad_apps
    from pillow_assistant.core.tools.builtin import cad_tool
    from pillow_assistant.core.tools.builtin.cad_tool import CadAppTool

    exe = tmp_path / "orca-slicer"
    exe.write_text("", "utf-8")
    monkeypatch.setattr(cad_apps, "locate", lambda key: exe)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        (tmp_path / "plate_1.gcode").write_text("G28", "utf-8")
        return subprocess.CompletedProcess(cmd, 0, "sliced", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = ToolContext(workspace=tmp_path)
    tool = CadAppTool()
    r = asyncio.run(tool({"app": "orcaslicer", "action": "run", "args": ["model.stl", "--slice", "0"]}, ctx))
    assert r.ok and seen["cmd"] == [str(exe), "model.stl", "--slice", "0"] and r.artifacts == ["plate_1.gcode"]
    assert asyncio.run(tool({"app": "orcaslicer", "action": "ensure"}, ctx)).ok

    launched = []
    monkeypatch.setattr(cad_tool, "_launch", lambda cmd, target: launched.append((cmd, target)))
    (tmp_path / "part.stl").write_text("solid", "utf-8")
    assert asyncio.run(tool({"app": "orcaslicer", "action": "open", "target": "part.stl"}, ctx)).ok
    link = "bambu-connect://import-file?path=%2Fx.gcode.3mf&name=x&version=1.0.0"
    assert asyncio.run(tool({"app": "bambu-connect", "action": "open", "target": link}, ctx)).ok
    assert launched[1] == (None, link)
    assert not asyncio.run(tool({"app": "orcaslicer", "action": "open", "target": "/etc/passwd"}, ctx)).ok
    assert not asyncio.run(tool({"app": "photoshop", "action": "ensure"}, ctx)).ok
