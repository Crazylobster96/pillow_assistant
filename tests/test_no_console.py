"""On Windows, console programs started by Pillow get no console window."""

import subprocess
import sys

import pytest

from pillow_assistant.core import no_console


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_popen_defaults_to_no_window_but_respects_explicit_flags(monkeypatch):
    seen = []
    monkeypatch.setattr(no_console, "_installed", False)
    original = subprocess.Popen.__init__

    def record(self, *args, **kwargs):
        seen.append(kwargs.get("creationflags", 0))
        raise RuntimeError("not started")

    monkeypatch.setattr(subprocess.Popen, "__init__", record)
    assert no_console.install()
    for flags in (None, subprocess.CREATE_NEW_PROCESS_GROUP, subprocess.DETACHED_PROCESS):
        kwargs = {} if flags is None else {"creationflags": flags}
        with pytest.raises(RuntimeError):
            subprocess.Popen(["cmd"], **kwargs)
    assert seen[0] & subprocess.CREATE_NO_WINDOW and seen[1] & subprocess.CREATE_NO_WINDOW
    assert seen[2] == subprocess.DETACHED_PROCESS  # a detached GUI launch is left alone
    monkeypatch.setattr(subprocess.Popen, "__init__", original)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_output_still_comes_back(monkeypatch):
    monkeypatch.setattr(no_console, "_installed", False)
    original = subprocess.Popen.__init__
    try:
        no_console.install()
        out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True).stdout
        assert out.strip() == "ok"
    finally:
        subprocess.Popen.__init__ = original
