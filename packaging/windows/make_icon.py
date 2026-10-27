"""Write the app's pillow icon as a multi-size .ico for PillowAssistant.exe and shortcuts.

Usage:  python make_icon.py <app-source-dir> <output.ico>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    app_dir, out = sys.argv[1], sys.argv[2]
    sys.path.insert(0, app_dir)
    from pillow_assistant.core.app_icon import artwork, write_ico

    source = Path(app_dir) / "pillow_assistant" / "ui" / "assets" / "pillow.png"
    if write_ico(source if source.is_file() else artwork(), Path(out)):
        Path(out + ".sha256").write_text(__import__("hashlib").sha256(source.read_bytes()).hexdigest(), "utf-8")
        return 0
    # No Pillow: let Qt draw it.
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
