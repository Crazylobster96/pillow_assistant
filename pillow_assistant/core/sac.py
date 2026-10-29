"""Windows Smart App Control (智能应用控制): when it is on, it blocks unsigned DLLs and
programs, and most of Pillow's native parts (the CAD kernel, numpy/scipy, FreeCAD, the
launcher) are unsigned. It cannot allow one app, so Pillow can only say what is happening.
"""

from __future__ import annotations

import re
import sys

# What a blocked load looks like in Python and Windows error text.
BLOCKED = re.compile(r"DLL load failed|WinError 4551|WinError 1260|Application Control policy|"
                     r"应用程序控制策略|blocked by group policy|组策略阻止|0xc0e90002", re.IGNORECASE)

OFF, ON, EVALUATION = 0, 1, 2


def state() -> int | None:
    """0 off, 1 on, 2 evaluation; None when it cannot be read (not Windows, older Windows)."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as key:
            return int(winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")[0])
    except (OSError, ValueError):
        return None


def blocked(text: str) -> bool:
    """Whether a failure looks like Smart App Control at work (the error reads like a blocked
    load and the control is on)."""
    return bool(BLOCKED.search(text or "")) and state() == ON
