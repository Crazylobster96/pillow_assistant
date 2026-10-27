"""The pillow artwork is the app icon, and an installed bundle's .ico follows it."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pillow_assistant.core import app_icon  # noqa: E402


def test_button_and_window_icons_use_the_artwork():
    from pillow_assistant.ui.icon_assets import PILLOW_IMAGE, app_icon as qt_icon, create_pillow_icon

    QApplication.instance() or QApplication([])
    assert PILLOW_IMAGE.is_file()
    pixmap = create_pillow_icon(96)
    assert (pixmap.width(), pixmap.height()) == (96, 96)
    assert {s.width() for s in qt_icon().availableSizes()} >= {16, 32, 48, 256}


def test_bundle_ico_is_rewritten_only_when_the_artwork_changes(tmp_path):
    (tmp_path / "PillowAssistant.exe").write_bytes(b"MZ")
    art = tmp_path / "pillow.png"
    Image.new("RGBA", (256, 256), (255, 255, 255, 255)).save(art)
    assert app_icon.refresh_bundle_icon(tmp_path, art)
    with Image.open(tmp_path / "PillowAssistant.ico") as ico:
        assert (256, 256) in ico.info["sizes"] and (16, 16) in ico.info["sizes"]
    assert not app_icon.refresh_bundle_icon(tmp_path, art)  # unchanged: left alone
    Image.new("RGBA", (256, 256), (0, 0, 255, 255)).save(art)
    assert app_icon.refresh_bundle_icon(tmp_path, art)
