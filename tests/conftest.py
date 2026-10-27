"""Keep every test out of the real user data directory.

The app keeps chat history, projects, settings and logs under ``~/.pillow``.
Tests that run the orchestrator with a fake model used to write their canned
turns ("这是回答", "好的，已完成。") into the developer's real chat history,
which the next real chat then read back as context. Each test now gets its own
home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home
