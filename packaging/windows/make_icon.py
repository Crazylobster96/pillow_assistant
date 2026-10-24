"""Render the app's pillow icon to a .ico for PillowAssistant.exe and shortcuts.

Usage:  python make_icon.py <app-source-dir> <output.ico>
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    app_dir, out = sys.argv[1], sys.argv[2]
    sys.path.insert(0, app_dir)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([])  # noqa: F841 - QPixmap needs a GUI application
    from pillow_assistant.ui.icon_assets import create_pillow_icon

    if not create_pillow_icon(256).save(out, "ICO"):
        print(f"could not write {out}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
