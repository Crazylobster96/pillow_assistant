"""Mainland China mirrors: CAD app downloads, cadgen's uv, and the Windows launcher's code updates."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import urllib.error
import zipfile
from pathlib import Path

import pytest

from pillow_assistant.capabilities import cad_apps
from pillow_assistant.core import mirrors

LAUNCHER = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "launcher.py"
spec = importlib.util.spec_from_file_location("bundle_launcher_mirrors", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

URL = "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD.7z"


def test_mirror_order_follows_the_mode(monkeypatch):
    monkeypatch.delenv("PILLOW_GITHUB_MIRRORS", raising=False)
    monkeypatch.setenv("PILLOW_MIRRORS", "auto")
    urls = mirrors.github_urls(URL)
    assert urls[0] == (URL, False) and urls[1] == ("https://ghfast.top/" + URL, True)
    assert mirrors.github_urls(URL, prefer_mirrors=True)[-1] == (URL, False)
    assert mirrors.github_urls("https://example.com/x") == [("https://example.com/x", False)]
    monkeypatch.setenv("PILLOW_MIRRORS", "cn")
    monkeypatch.setenv("PILLOW_GITHUB_MIRRORS", "https://my.proxy, http://insecure/")
    assert mirrors.github_urls(URL) == [("https://my.proxy/" + URL, True), (URL, False)]
    assert mirrors.pypi_indexes()[-1] is None
    monkeypatch.setenv("PILLOW_MIRRORS", "off")
    assert mirrors.github_urls(URL) == [(URL, False)] and mirrors.pypi_indexes() == [None]


def test_uv_retries_point_index_and_python_downloads_at_mirrors(monkeypatch):
    monkeypatch.setenv("PILLOW_MIRRORS", "auto")
    monkeypatch.delenv("PILLOW_PYPI_MIRRORS", raising=False)
    monkeypatch.delenv("PILLOW_GITHUB_MIRRORS", raising=False)
    envs = list(mirrors.uv_environments({"A": "1"}))
    assert envs[0] == {"A": "1"}
    assert envs[1]["UV_DEFAULT_INDEX"] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert envs[1]["UV_PYTHON_INSTALL_MIRROR"].endswith("/https://github.com/astral-sh/python-build-standalone"
                                                        "/releases/download")


def _asset(data: bytes) -> dict:
    return {"tag": "1.1.3", "name": "FreeCAD.7z", "url": URL,
            "digest": "sha256:" + hashlib.sha256(data).hexdigest()}


def test_mirrored_download_must_match_githubs_digest(monkeypatch, tmp_path):
    monkeypatch.setenv("PILLOW_MIRRORS", "auto")
    monkeypatch.delenv("PILLOW_GITHUB_MIRRORS", raising=False)
    tried = []

    def download(url, dest):
        tried.append(url)
        if url == URL:
            raise urllib.error.URLError("blocked")
        dest.write_bytes(b"tampered" if "ghfast" in url else b"real")

    cad_apps.fetch_asset(_asset(b"real"), tmp_path / "f.7z", download)
    assert (tmp_path / "f.7z").read_bytes() == b"real" and len(tried) == 3
    with pytest.raises(cad_apps.CadAppError, match="does not match"):
        cad_apps.fetch_asset(_asset(b"other"), tmp_path / "g.7z", download)
    tried.clear()
    with pytest.raises(cad_apps.CadAppError, match="blocked"):
        cad_apps.fetch_asset({**_asset(b"real"), "digest": ""}, tmp_path / "h.7z", download)
    assert tried == [URL]  # no digest to check: never through a mirror


def test_install_falls_back_to_pins_and_mirrors_when_github_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("PILLOW_CAD_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setenv("PILLOW_MIRRORS", "auto")
    monkeypatch.delenv("PILLOW_GITHUB_MIRRORS", raising=False)
    monkeypatch.setattr(cad_apps, "platform_key", lambda: "windows-x64")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("OrcaSlicer/orca-slicer.exe", "exe")
    data = buf.getvalue()
    asset = {**_asset(data), "name": "OrcaSlicer_Windows_V2_x64_portable.zip",
             "url": "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2/o.zip"}
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "pins.json").write_text(json.dumps({"orcaslicer": {"windows-x64": asset}}), "utf-8")
    tried = []

    def blocked_api(url):
        raise urllib.error.URLError("api blocked")

    def download(url, dest):
        tried.append(url)
        dest.write_bytes(data)

    monkeypatch.setattr(cad_apps.sys, "platform", "win32")
    result = cad_apps.install("orcaslicer", get_json=blocked_api, download=download)
    assert result["version"] == "1.1.3" and tried[0].startswith("https://ghfast.top/")
    assert cad_apps.pin("windows-x64", ["orcaslicer"], get_json=lambda url: [{
        "tag_name": "v2", "assets": [{"name": asset["name"], "browser_download_url": asset["url"],
                                      "digest": asset["digest"]}]}]) == {
        "orcaslicer": {"windows-x64": {"tag": "v2", "name": asset["name"], "url": asset["url"],
                                       "digest": asset["digest"]}}}


def _repo_archive(tmp_path: Path) -> tuple[bytes, str]:
    repo = tmp_path / "repo"
    (repo / "pillow_assistant").mkdir(parents=True)
    (repo / "pillow_assistant" / "app.py").write_text("VERSION = 1\n")
    (repo / "requirements.txt").write_text("x\n")
    (repo / "run.bat").write_text("echo hi\n")
    (repo / "tool.sh").write_text("#!/bin/sh\n")
    (repo / ".gitattributes").write_text("*.bat text eol=crlf\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    (repo / "tool.sh").chmod(0o755)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    out = tmp_path / "a.zip"
    subprocess.run(git + ["archive", "--format=zip", "--prefix=repo-abc/", "-o", str(out), "HEAD"], check=True)
    tree = subprocess.check_output(git + ["rev-parse", "HEAD^{tree}"], text=True).strip()
    return out.read_bytes(), tree


def test_launcher_takes_a_mirrored_archive_only_when_its_git_tree_matches(tmp_path, monkeypatch):
    archive, tree = _repo_archive(tmp_path)
    assert b"echo hi\r\n" in zipfile.ZipFile(io.BytesIO(archive)).read("repo-abc/run.bat")
    assert launcher.archive_tree_sha(archive) == tree
    paths = launcher.Paths(tmp_path / "bundle")
    paths.app.mkdir(parents=True)
    launcher.stamp_version(paths, "a" * 40, "main")
    monkeypatch.setattr(launcher, "_get_json", lambda url, t: {
        "sha": "b" * 40, "commit": {"tree": {"sha": tree}}} if "/commits/" in url else {"status": "ahead"})
    cfg = dict(launcher.DEFAULT_CONFIG)
    tried = []

    def download(url, timeout):
        tried.append(url)
        if url.startswith(launcher.API):
            raise urllib.error.URLError("codeload blocked")
        if "ghfast" in url:
            bad = io.BytesIO()
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("repo-abc/pillow_assistant/app.py", "import evil\n")
                zf.writestr("repo-abc/requirements.txt", "x\n")
            return bad.getvalue()
        return archive

    meta = launcher.stage_update(paths, cfg, download=download)
    assert meta["sha"] == "b" * 40 and len(tried) == 3 and "gh-proxy.com" in tried[2]
    assert (paths.staged / "pillow_assistant" / "app.py").read_text() == "VERSION = 1\n"
    assert launcher.pip_indexes({**cfg, "mirrors": "cn"})[-1] is None
    assert launcher.download_sources({**cfg, "mirrors": "off"}, {"zip_url": "z", "archive_url": "a",
                                                                 "tree": tree}) == [("z", False)]
