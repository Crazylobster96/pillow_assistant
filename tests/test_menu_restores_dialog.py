"""The radial menu pushes the question dialog aside; it goes back as soon as the menu closes,
and the window the menu opens (projects) is placed clear of it."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint  # noqa: E402

from pillow_assistant.ui.window_coordinator import WindowCoordinator, when_closed  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _setup():
    anchor, bar, menu = (QtWidgets.QWidget() for _ in range(3))
    anchor.setGeometry(100, 100, 60, 60)
    bar.setGeometry(90, 180, 300, 80)
    for widget in (anchor, bar, menu):
        widget.show()
    coordinator = WindowCoordinator(anchor, storage=None)
    coordinator._slots.update(quick=bar, radial=menu)
    return coordinator, bar, menu


def test_the_dialog_is_back_before_the_picked_window_opens(app):
    coordinator, bar, menu = _setup()
    home = bar.pos()
    coordinator.make_room_for_menu()
    assert bar.pos() != home
    when_closed(menu, coordinator.after_menu_closed)
    menu.close()  # the menu closes, then runs the picked action
    assert bar.pos() == home
    projects = QtWidgets.QWidget()
    projects.resize(500, 300)
    coordinator.place_near_icon(projects, fit=False)
    assert not projects.frameGeometry().intersects(bar.frameGeometry())
    assert (projects.width(), projects.height()) == (500, 300)  # its own size is kept


def test_a_dialog_the_user_moved_stays_put(app):
    coordinator, bar, menu = _setup()
    coordinator.make_room_for_menu()
    bar.move(QPoint(50, 50))
    coordinator.restore_room_for_menu()
    assert bar.pos() == QPoint(50, 50)
