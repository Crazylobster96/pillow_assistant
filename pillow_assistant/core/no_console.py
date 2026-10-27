"""Run console programs without flashing a console window (Windows).

Pillow runs under pythonw.exe, which has no console. Each console program it
starts (the run_python sandbox, CLI tools, browsers, converters)
therefore opened a window of its own. ``install`` makes every
``subprocess.Popen`` (asyncio's too) default to CREATE_NO_WINDOW: the child
gets a hidden console, which its own children share. GUI apps ignore the flag;
callers that ask for a console or detach (CREATE_NEW_CONSOLE, DETACHED_PROCESS)
are left as they are.
"""

from __future__ import annotations

import subprocess
import sys

_installed = False


def install() -> bool:
    """Patch Popen once; True when it is (or already was) in place."""
    global _installed
    if sys.platform != "win32":
        return False
    if _installed:
        return True
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    explicit = (getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
                | getattr(subprocess, "DETACHED_PROCESS", 0x8))
    original = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & explicit:
            kwargs["creationflags"] = flags | no_window
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__
    _installed = True
    return True
