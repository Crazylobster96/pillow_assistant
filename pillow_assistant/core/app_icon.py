"""The app icon outside Qt: the Windows taskbar identity and the bundle's .ico.

The Windows bundle's shortcuts point at ``<bundle>/PillowAssistant.ico``, which is
written once when the full package is built. Code updates replace only ``app/``,
so the app rewrites that .ico when the pillow artwork it ships has changed.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

APP_ID = "PillowAssistant.Desktop"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def artwork() -> Path:
    return Path(__file__).resolve().parents[1] / "ui" / "assets" / "pillow.png"


def bundle_root() -> Optional[Path]:
    """The Windows bundle folder (it holds PillowAssistant.exe and app/), if running from one."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / "PillowAssistant.exe").is_file() else None


def set_app_user_model_id() -> None:
    """Group Pillow's windows under its own taskbar entry and icon (not pythonw.exe's)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass


def write_ico(source: Path, target: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGBA").save(target, format="ICO", sizes=ICO_SIZES)
        return True
    except Exception as exc:  # noqa: BLE001 - missing Pillow, unwritable folder
        log.warning("could not write %s: %s", target, exc)
        return False


def refresh_bundle_icon(root: Optional[Path] = None, source: Optional[Path] = None) -> bool:
    """Rewrite ``PillowAssistant.ico`` when the shipped artwork changed; True if it did."""
    root = root or bundle_root()
    source = source or artwork()
    if root is None or not source.is_file():
        return False
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    stamp = root / "PillowAssistant.ico.sha256"
    ico = root / "PillowAssistant.ico"
    try:
        if ico.is_file() and stamp.read_text("utf-8").strip() == digest:
            return False
    except OSError:
        pass
    if not write_ico(source, ico):
        return False
    try:
        stamp.write_text(digest, "utf-8")
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
        except (AttributeError, OSError):
            pass
    return True
