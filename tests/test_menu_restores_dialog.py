"""The radial menu pushes the question dialog aside; it goes back once the menu and what it
opened (the projects window) are closed."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint  # noqa: E402

from pillow_assistant.ui.window_coordinator import WindowCoordinator  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _settle(app):
    for _ in range(3):
        app.processEvents()


def test_the_dialog_returns_after_the_menu_and_its_window_close(app):
    anchor, bar, menu, projects = (QtWidgets.QWidget() for _ in range(4))
    anchor.setGeometry(400, 300, 60, 60)
    bar.setGeometry(380, 380, 300, 80)
    for widget in (anchor, bar, menu):
        widget.show()
    coordinator = WindowCoordinator(anchor, storage=None)
    coordinator._slots.update(quick=bar, radial=menu)
    home = bar.pos()

    coordinator.make_room_for_menu()
    assert bar.pos() != home
    from pillow_assistant.ui.window_coordinator import when_closed
    when_closed(menu, coordinator.after_menu_closed)
    coordinator._slots["projects"] = projects  # the menu opened the projects window
    projects.show()
    menu.close()
    _settle(app)
    assert bar.pos() != home  # still out of the way while the projects window is open
    projects.close()
    _settle(app)
    assert bar.pos() == home


def test_a_dialog_the_user_moved_stays_put(app):
    anchor, bar, menu = (QtWidgets.QWidget() for _ in range(3))
    anchor.setGeometry(400, 300, 60, 60)
    bar.setGeometry(380, 380, 300, 80)
    for widget in (anchor, bar, menu):
        widget.show()
    coordinator = WindowCoordinator(anchor, storage=None)
    coordinator._slots.update(quick=bar, radial=menu)
    coordinator.make_room_for_menu()
    bar.move(QPoint(50, 50))
    coordinator.restore_room_for_menu()
    assert bar.pos() == QPoint(50, 50)
