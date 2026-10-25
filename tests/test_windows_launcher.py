"""Offline Windows bundle launcher: staging and applying GitHub updates."""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "launcher.py"
spec = importlib.util.spec_from_file_location("bundle_launcher", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

OLD, NEW = "a" * 40, "b" * 40


def _source_tree(root: Path, marker: str, requirements: str = "PySide6>=6.6\n") -> None:
    (root / "pillow_assistant").mkdir(parents=True)
    (root / "pillow_assistant" / "app.py").write_text(f"VERSION = {marker!r}\n", encoding="utf-8")
    (root / "requirements.txt").write_text(requirements, encoding="utf-8")
    (root / "packaging" / "windows").mkdir(parents=True)
    (root / "packaging" / "windows" / "launcher.py").write_text(f"# {marker}\n", encoding="utf-8")


def _zipball(marker: str, requirements: str = "PySide6>=6.6\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        top = f"Crazylobster96-pillow_assistant-{marker[:7]}/"
        zf.writestr(top + "pillow_assistant/app.py", f"VERSION = {marker!r}\n")
        zf.writestr(top + "requirements.txt", requirements)
        zf.writestr(top + "packaging/windows/launcher.py", f"# {marker}\n")
    return buf.getvalue()


@pytest.fixture
def bundle(tmp_path):
    paths = launcher.Paths(tmp_path)
    _source_tree(paths.app, OLD)
    (paths.app / "data" / "logs").mkdir(parents=True)
    (paths.app / "data" / "logs" / "llm.jsonl").write_text("{}", encoding="utf-8")
    launcher.stamp_version(paths, OLD, "main")
    (tmp_path / "launcher.pyw").write_text("# old launcher\n", encoding="utf-8")
    return paths


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub API: ``state`` controls the newest commit and compare result."""
    state = {"sha": NEW, "compare": "ahead", "tag": "v1.0.0", "calls": []}

    def fake_get_json(url, timeout):
        state["calls"].append(url)
        if url.endswith("/releases/latest"):
            return {"tag_name": state["tag"]}
        if "/compare/" in url:
            if state["compare"] == 404:
                raise urllib.error.HTTPError(url, 404, "not found", {}, None)
            return {"status": state["compare"]}
        if "/commits/" in url:
            return {"sha": state["sha"], "commit": {"committer": {"date": "2026-10-24T00:00:00Z"}}}
        raise AssertionError(url)

    monkeypatch.setattr(launcher, "_get_json", fake_get_json)
    return state


def _cfg(**overrides):
    cfg = dict(launcher.DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


def test_stage_then_apply_swaps_app_and_keeps_data(bundle, github):
    meta = launcher.stage_update(bundle, _cfg(), download=lambda url, t: _zipball(NEW))

    assert meta["sha"] == NEW and meta["ref"] == "main"
    assert (bundle.staged / "pillow_assistant" / "app.py").is_file()
    assert json.loads(bundle.staged_marker.read_text())["sha"] == NEW
    # Nothing changes for the running app until the next start.
    assert launcher.current_version(bundle)["sha"] == OLD

    installs = []
    assert launcher.apply_staged(bundle, install=lambda p, r: installs.append(r) or True)

    assert installs == []  # requirements unchanged -> no pip run
    assert launcher.current_version(bundle)["sha"] == NEW
    assert NEW in (bundle.app / "pillow_assistant" / "app.py").read_text()
    assert (bundle.app / "data" / "logs" / "llm.jsonl").is_file()
    assert (bundle.previous / "pillow_assistant" / "app.py").is_file()
    assert (bundle.root / "launcher.pyw").read_text() == f"# {NEW}\n"
    assert not bundle.staged_marker.exists()
    assert not list(bundle.updates.glob("download-*"))


def test_up_to_date_does_not_download(bundle, github):
    github["sha"] = OLD

    def boom(url, t):
        raise AssertionError("should not download")

    assert launcher.stage_update(bundle, _cfg(), download=boom) is None


def test_never_downgrades_to_older_remote(bundle, github):
    github["compare"] = "behind"
    assert launcher.stage_update(bundle, _cfg(), download=lambda u, t: _zipball(NEW)) is None


def test_unknown_local_commit_trusts_remote(bundle, github):
    github["compare"] = 404
    assert launcher.stage_update(bundle, _cfg(), download=lambda u, t: _zipball(NEW))


def test_release_channel_resolves_tag(bundle, github):
    meta = launcher.stage_update(bundle, _cfg(channel="release"),
                                 download=lambda u, t: _zipball(NEW))
    assert meta["ref"] == "v1.0.0"
    assert any(c.endswith("/commits/v1.0.0") for c in github["calls"])


def test_changed_requirements_are_installed_first(bundle, github):
    launcher.stage_update(bundle, _cfg(),
                          download=lambda u, t: _zipball(NEW, "PySide6>=6.6\nrich\n"))
    installs = []
    assert launcher.apply_staged(bundle, install=lambda p, r: installs.append(r) or True)
    assert len(installs) == 1 and installs[0].name == "requirements.txt"


def test_failed_dependency_install_keeps_current_version(bundle, github):
    launcher.stage_update(bundle, _cfg(),
                          download=lambda u, t: _zipball(NEW, "PySide6>=6.6\nrich\n"))
    assert not launcher.apply_staged(bundle, install=lambda p, r: False)
    assert launcher.current_version(bundle)["sha"] == OLD
    assert bundle.staged_marker.exists()  # retried on the next start


def test_requirements_hash_ignores_line_endings(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"PySide6>=6.6\nnumpy\n")
    b.write_bytes(b"PySide6>=6.6\r\nnumpy\r\n")
    assert launcher.requirements_hash(a) == launcher.requirements_hash(b)


def test_rejects_archive_that_is_not_the_app(bundle, github):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x/readme.md", "hi")
    with pytest.raises(ValueError):
        launcher.stage_update(bundle, _cfg(), download=lambda u, t: buf.getvalue())
    assert launcher.current_version(bundle)["sha"] == OLD
    assert not bundle.staged.exists()


def test_offline_check_fails_quietly(bundle, monkeypatch):
    def offline(url, timeout):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(launcher, "_get_json", offline)
    assert launcher.check_once(bundle, _cfg(), force=True) is None
    assert "no network" in json.loads(bundle.state.read_text())["last_error"]


def test_check_interval_is_respected(bundle, github, monkeypatch):
    monkeypatch.setattr(launcher, "_http_get", lambda url, t, accept=None: _zipball(NEW))
    assert launcher.check_once(bundle, _cfg(), force=True)["sha"] == NEW
    github["calls"].clear()
    assert launcher.check_once(bundle, _cfg(check_interval_hours=6)) is None
    assert github["calls"] == []


def test_rollback_restores_previous(bundle, github):
    launcher.stage_update(bundle, _cfg(), download=lambda u, t: _zipball(NEW))
    launcher.apply_staged(bundle, install=lambda p, r: True)
    assert launcher.rollback(bundle)
    assert launcher.current_version(bundle)["sha"] == OLD


def test_disable_via_config_and_env(bundle, monkeypatch):
    bundle.config.write_text(json.dumps({"enabled": False, "_comment": "x"}), encoding="utf-8")
    assert launcher.load_config(bundle)["enabled"] is False
    bundle.config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PILLOW_NO_UPDATE", "1")
    assert launcher.load_config(bundle)["enabled"] is False


def test_bundled_models_dir_is_preferred(tmp_path, monkeypatch):
    from pillow_assistant.core import asr

    (tmp_path / "SenseVoiceSmall").mkdir()
    monkeypatch.setenv("PILLOW_MODELS_DIR", str(tmp_path))
    assert asr._local_model("SenseVoiceSmall", "iic/SenseVoiceSmall") == str(tmp_path / "SenseVoiceSmall")
    assert asr._local_model("fsmn-vad", "fsmn-vad") == "fsmn-vad"


def test_shortcuts_created_once_and_after_move(bundle):
    (bundle.root / "PillowAssistant.exe").write_bytes(b"MZ")
    calls = []

    def fake_run(target, workdir, icon):
        calls.append((target, workdir, icon))
        return ["Desktop/Pillow Assistant.lnk", "Programs/Pillow Assistant.lnk"]

    assert launcher.create_shortcuts(bundle, run=fake_run)
    target, workdir, icon = calls[0]
    assert target == bundle.root / "PillowAssistant.exe" and workdir == bundle.root
    assert icon.endswith("PillowAssistant.exe,0")
    # A shortcut the user deleted is not recreated on later starts...
    assert launcher.create_shortcuts(bundle, run=fake_run) == []
    # ...unless forced, or the bundle folder moved.
    (bundle.root / "PillowAssistant.ico").write_bytes(b"\0")
    assert launcher.create_shortcuts(bundle, force=True, run=fake_run)
    assert calls[-1][2].endswith("PillowAssistant.ico,0")
    state = json.loads(bundle.state.read_text())
    state["shortcuts_for"] = "D:/old-location"
    bundle.state.write_text(json.dumps(state))
    assert launcher.create_shortcuts(bundle, run=fake_run)
    assert len(calls) == 3


def test_shortcut_failure_is_not_recorded(bundle):
    (bundle.root / "PillowAssistant.exe").write_bytes(b"MZ")

    def failing(target, workdir, icon):
        raise OSError("COM unavailable")

    assert launcher.create_shortcuts(bundle, run=failing) == []
    assert "shortcuts_for" not in launcher._read_json(bundle.state)


def test_no_shortcuts_outside_a_bundle(bundle):
    assert launcher.create_shortcuts(bundle, run=lambda *a: ["x"]) == []


def _update_to_new(bundle):
    launcher.stage_update(bundle, _cfg(), download=lambda u, t: _zipball(NEW))
    assert launcher.apply_staged(bundle, install=lambda p, r: True)
    assert json.loads(bundle.probation.read_text())["sha"] == NEW


def test_manual_rollback_skips_bad_version_until_a_newer_commit(bundle, github):
    _update_to_new(bundle)
    assert launcher.rollback(bundle)
    assert launcher.current_version(bundle)["sha"] == OLD
    assert (bundle.app / "data" / "logs" / "llm.jsonl").is_file()  # runtime data comes back
    assert not bundle.probation.exists()
    assert launcher.skipped_shas(bundle) == [NEW]

    def boom(url, t):
        raise AssertionError("must not download a rolled-back version")

    assert launcher.stage_update(bundle, _cfg(), download=boom) is None
    github["sha"] = "c" * 40  # a fix lands upstream
    assert launcher.stage_update(bundle, _cfg(), download=lambda u, t: _zipball("c" * 40))["sha"] == "c" * 40

    launcher.clear_skipped(bundle)
    assert launcher.skipped_shas(bundle) == []


def test_crash_on_probation_rolls_back_and_relaunches(bundle, github):
    _update_to_new(bundle)
    assert launcher.begin_probation_start(bundle)
    relaunched = []

    def broken_app():
        raise ImportError("new version is broken")

    with pytest.raises(ImportError):
        launcher.run_app(bundle, broken_app, True, relaunch=lambda: relaunched.append(1))
    assert launcher.current_version(bundle)["sha"] == OLD
    assert relaunched == [1]
    assert json.loads(bundle.state.read_text())["last_rollback"]["sha"] == NEW


def test_clean_exit_on_probation_keeps_new_version(bundle, github):
    _update_to_new(bundle)
    assert launcher.begin_probation_start(bundle)
    assert launcher.run_app(bundle, lambda: 0, True) == 0
    assert not bundle.probation.exists()
    assert launcher.current_version(bundle)["sha"] == NEW
    assert not launcher.begin_probation_start(bundle)


def test_repeated_hard_crashes_roll_back_on_next_start(bundle, github):
    _update_to_new(bundle)
    for _ in range(launcher.MAX_UNHEALTHY_STARTS):
        assert launcher.begin_probation_start(bundle)  # process dies before becoming healthy
    assert not launcher.begin_probation_start(bundle)
    assert launcher.current_version(bundle)["sha"] == OLD
    assert launcher.skipped_shas(bundle) == [NEW]


def test_crash_outside_probation_does_not_roll_back(bundle, github):
    _update_to_new(bundle)
    launcher.mark_healthy(bundle)
    with pytest.raises(RuntimeError):
        launcher.run_app(bundle, lambda: (_ for _ in ()).throw(RuntimeError("bug")), False)
    assert launcher.current_version(bundle)["sha"] == NEW
