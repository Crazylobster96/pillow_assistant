"""Anatomy integration: vendored Skill, Node/Chrome discovery, workdir, and the two tools.

A real figure is built (and screenshotted when a Chrome is found) whenever a
Node 20.10+ is on PATH; the Node download is only exercised with a fake.
"""

import asyncio
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from pillow_assistant.capabilities import anatomy
from pillow_assistant.capabilities.skill_registry import CapabilitySkillRegistry
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.anatomy_tool import AnatomyReferenceTool, AnatomyTool
from pillow_assistant.core.tools.builtin.skill_tool import SkillTool

FIGURE = """import { writeFileSync } from "node:fs";
import * as k from "../../kit/iso-kit.mjs";
console.log(typeof k.solidSvg, typeof k.pageHtml);
writeFileSync(new URL("./tiny.html", import.meta.url), "<!doctype html><title>tiny</title>");
"""


def test_vendored_skill_and_upstream_pin():
    info = anatomy.upstream_info()
    assert info["repo"] == "wheresryan22/anatomy"
    assert info["version"] and info["commit"] and info["node_min"]
    root = anatomy.skill_dir()
    for rel in ("SKILL.md", "LICENSE", "kit/iso-kit.mjs", "scripts/capture.mjs", "scripts/inline-kit.mjs",
                "references/craft.md", "examples/dial-indicator/build.mjs"):
        assert (root / rel).is_file(), rel
    assert not list(root.glob("examples/*/*.html")) and not list(root.glob("examples/*/*.pdf"))
    assert not (root / ".github").exists() and not list(root.glob("scripts/*.test.mjs"))


def test_registry_loads_anatomy_and_users_can_override(tmp_path):
    skills = {s.name: s for s in CapabilitySkillRegistry(user_root=tmp_path / "none").load()}
    assert skills["anatomy"].source_kind == anatomy.SOURCE_KIND
    assert "isometric" in skills["anatomy"].description
    user = tmp_path / "user"
    (user / "anatomy").mkdir(parents=True)
    (user / "anatomy" / "SKILL.md").write_text("---\nname: anatomy\ndescription: mine\n---\nbody", "utf-8")
    skills = {s.name: s for s in CapabilitySkillRegistry(user_root=user).load()}
    assert skills["anatomy"].source_kind == "user"


def test_apply_skill_prepends_pillow_adapter_note(tmp_path):
    skills = CapabilitySkillRegistry(user_root=tmp_path / "none").load()
    result = asyncio.run(SkillTool(skills)({"name": "anatomy"}, ToolContext(workspace=tmp_path)))
    assert result.ok
    assert "anatomy_reference" in result.text and "figures/" in result.text
    assert "# Anatomy" in result.text


def test_prepare_workdir_copies_kit_and_refreshes_on_new_version(tmp_path):
    root = tmp_path / "skills"
    (root / "anatomy" / "kit").mkdir(parents=True)
    (root / "anatomy" / "scripts").mkdir()
    (root / "anatomy" / "kit" / "iso-kit.mjs").write_text("v1", "utf-8")
    (root / "UPSTREAM.json").write_text(json.dumps({"version": "1.0.0", "commit": "a"}), "utf-8")
    ws = tmp_path / "ws"
    work = anatomy.prepare_workdir(ws, root)
    assert work == ws / "anatomy" and (work / "figures").is_dir()
    assert (work / "kit" / "iso-kit.mjs").read_text("utf-8") == "v1"
    (root / "anatomy" / "kit" / "iso-kit.mjs").write_text("v2", "utf-8")
    anatomy.prepare_workdir(ws, root)
    assert (work / "kit" / "iso-kit.mjs").read_text("utf-8") == "v1"  # same release: left alone
    (root / "UPSTREAM.json").write_text(json.dumps({"version": "1.1.0", "commit": "b"}), "utf-8")
    anatomy.prepare_workdir(ws, root)
    assert (work / "kit" / "iso-kit.mjs").read_text("utf-8") == "v2"


def test_find_chrome_uses_playwright_headless_shell(monkeypatch, tmp_path):
    shell = tmp_path / "chromium_headless_shell-1200" / "chrome-headless-shell-win64" / "chrome-headless-shell.exe"
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b"")
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert anatomy.find_chrome() == shell
    full = tmp_path / "chromium-1200" / "chrome-win" / "chrome.exe"
    full.parent.mkdir(parents=True)
    full.write_bytes(b"")
    assert anatomy.find_chrome() == full  # a full Chromium wins over the shell


def test_node_urls_follow_mirror_mode(monkeypatch):
    name = anatomy.NODE_ARCHIVES["windows-x64"][0]
    monkeypatch.setenv("PILLOW_MIRRORS", "auto")
    urls = anatomy.node_urls(name)
    assert urls[0] == f"https://nodejs.org/dist/v{anatomy.NODE_VERSION}/{name}"
    assert any("npmmirror.com" in u for u in urls[1:])
    monkeypatch.setenv("PILLOW_MIRRORS", "cn")
    assert "npmmirror.com" in anatomy.node_urls(name)[0]
    monkeypatch.setenv("PILLOW_MIRRORS", "off")
    assert anatomy.node_urls(name) == [f"https://nodejs.org/dist/v{anatomy.NODE_VERSION}/{name}"]


def _fake_node_archive(stem: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"#!/bin/sh\necho v22.23.3\n"
        info = tarfile.TarInfo(f"{stem}/bin/node")
        info.size, info.mode = len(data), 0o755
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_node_checks_hash_and_falls_back_to_mirror(monkeypatch, tmp_path):
    stem = "node-v22.23.3-linux-x64"
    good = _fake_node_archive(stem)
    monkeypatch.setitem(anatomy.NODE_ARCHIVES, "linux-x64", (stem + ".tar.gz", hashlib.sha256(good).hexdigest()))
    monkeypatch.setenv("PILLOW_NODE_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("PILLOW_MIRRORS", "auto")
    tried = []

    def download(url, dest):
        tried.append(url)
        if url.startswith("https://nodejs.org/"):
            raise OSError("blocked")
        dest.write_bytes(b"tampered" if "huaweicloud" in url else good)

    monkeypatch.setenv("PILLOW_NODE_MIRRORS", "https://mirrors.huaweicloud.com/nodejs/,https://npmmirror.com/mirrors/node/")
    exe = anatomy.install_node(download, plat="linux-x64")
    assert exe == tmp_path / "node" / stem / "bin" / "node"
    assert [u.split("/")[2] for u in tried] == ["nodejs.org", "mirrors.huaweicloud.com", "npmmirror.com"]
    assert not list((tmp_path / "node").glob("*.tar.gz"))


def test_anatomy_tool_rejects_bad_input(tmp_path):
    ctx = ToolContext(workspace=tmp_path)
    tool = AnatomyTool()
    assert not asyncio.run(tool({"args": []}, ctx)).ok
    assert not asyncio.run(tool({"args": ["-e", "1"]}, ctx)).ok
    assert not asyncio.run(tool({"args": ["../../etc/x.mjs"]}, ctx)).ok
    assert not asyncio.run(tool({"args": ["figures/missing/build.mjs"]}, ctx)).ok
    assert not asyncio.run(tool({"action": "open", "path": "../x.html"}, ctx)).ok
    assert not asyncio.run(tool({"action": "nope"}, ctx)).ok
    off = asyncio.run(tool({"args": ["figures/a.mjs"]}, ToolContext(workspace=tmp_path, allow_cli=False)))
    assert not off.ok


def test_anatomy_tool_installs_node_when_missing(monkeypatch, tmp_path):
    from pillow_assistant.core.tools.builtin import anatomy_tool

    work = anatomy.prepare_workdir(tmp_path)
    (work / "figures" / "a").mkdir()
    (work / "figures" / "a" / "build.mjs").write_text("", "utf-8")
    calls = []
    monkeypatch.setattr(anatomy, "find_node", lambda: None)
    monkeypatch.setattr(anatomy, "install_node", lambda: Path("/opt/node/bin/node"))

    class Done:
        returncode, stdout, stderr = 0, "built", ""

    monkeypatch.setattr(anatomy_tool.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or Done())
    result = asyncio.run(AnatomyTool()({"args": ["node", "figures/a/build.mjs", "--light"]}, ToolContext(workspace=tmp_path)))
    assert result.ok, result.text
    cmd, kw = calls[0]
    assert cmd == [str(Path("/opt/node/bin/node")), "figures/a/build.mjs", "--light"]
    assert kw["cwd"] == str(work) and kw["shell"] is False
    assert anatomy.NODE_VERSION in result.text


def test_anatomy_reference_reads_skill_files(tmp_path):
    tool = AnatomyReferenceTool()
    ctx = ToolContext(workspace=tmp_path)
    status = asyncio.run(tool({}, ctx))
    assert status.ok and anatomy.upstream_info()["version"] in status.text
    assert "iso-kit.mjs" in asyncio.run(tool({"path": "kit"}, ctx)).text
    assert asyncio.run(tool({"path": "anatomy/references/craft.md"}, ctx)).ok
    assert not asyncio.run(tool({"path": "../UPSTREAM.json"}, ctx)).ok
    assert not asyncio.run(tool({"path": "references/missing.md"}, ctx)).ok


@pytest.mark.skipif(anatomy.find_node() is None, reason="needs Node.js 20.10+ on PATH")
def test_builds_a_real_figure_with_the_vendored_kit(tmp_path):
    ctx = ToolContext(workspace=tmp_path)
    work = anatomy.prepare_workdir(tmp_path)
    fig = work / "figures" / "tiny"
    fig.mkdir()
    (fig / "build.mjs").write_text(FIGURE, "utf-8")
    result = asyncio.run(AnatomyTool()({"args": ["figures/tiny/build.mjs"]}, ctx))
    assert result.ok, result.text
    assert "function function" in result.text
    assert "anatomy/figures/tiny/tiny.html" in result.artifacts

    shutil.copytree(anatomy.skill_dir() / "examples" / "dial-indicator", work / "figures" / "dial")
    built = asyncio.run(AnatomyTool()({"args": ["figures/dial/build.mjs", "--light"]}, ctx))
    assert built.ok, built.text
    page = next(p for p in built.artifacts if p.endswith(".html"))
    if anatomy.find_chrome() is None and not shutil.which("google-chrome") and not shutil.which("chromium"):
        return
    rel = page.split("/", 1)[1]
    shot = asyncio.run(AnatomyTool()({"args": ["scripts/capture.mjs", rel, "--out", "shots/dial.png"]}, ctx))
    assert shot.ok, shot.text
    assert (work / "shots" / "dial.png").stat().st_size > 10_000
