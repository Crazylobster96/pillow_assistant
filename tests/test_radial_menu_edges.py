"""The round menu stays centred on the icon near a screen edge: its fan turns or narrows
instead of the ring sliding off the icon (which put buttons on top of it)."""

import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from pillow_assistant.ui.radial_menu import RadialMenu  # noqa: E402

ITEMS = [(name, lambda: None) for name in ("项目", "Memory", "配置", "关闭展示", "清除引用", "退出")]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.mark.parametrize("where", ["left", "top_left", "middle"])
def test_the_ring_stays_on_the_icon_and_every_button_is_on_screen(app, where):
    area = QGuiApplication.primaryScreen().availableGeometry()
    centre = {"left": QPoint(area.left() + 40, area.center().y()),
              "top_left": QPoint(area.left() + 40, area.top() + 40),
              "middle": area.center()}[where]
    menu = RadialMenu(ITEMS, centre)
    ring = menu.geometry().center()
    assert abs(ring.x() - centre.x()) <= 1 and abs(ring.y() - centre.y()) <= 1
    origin = menu.geometry().topLeft()
    for bx, by in menu._centers:
        x, y = origin.x() + bx, origin.y() + by
        assert area.contains(QPoint(int(x), int(y)))  # on screen
        assert math.hypot(x - centre.x(), y - centre.y()) >= RadialMenu.RADIUS - 1  # clear of the icon
    menu.close()
