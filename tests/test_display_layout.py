"""The dialog returns next to the floating button when the display tiles close."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

def test_dialog_returns_next_to_the_button_when_the_displays_close():
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QWidget

    from pillow_assistant.ui.window_coordinator import WindowCoordinator

    app = QApplication.instance() or QApplication([])
    anchor = QWidget()
    anchor.setGeometry(100, 100, 48, 48)
    anchor.show()
    coordinator = WindowCoordinator(anchor, None)
    bar = QWidget()
    bar.show()
    coordinator.set_window("quick", bar)
    tiles = [QWidget(), QWidget()]
    for tile in tiles:
        tile.show()
        coordinator.add_multi_window(tile)
    bar.move(QPoint(700, 900))  # parked under the tiles
    coordinator.park_quick_for_displays()

    coordinator.remove_multi_window(tiles[0])
    assert bar.pos() == QPoint(700, 900)  # one display still open
    coordinator.remove_multi_window(tiles[1])
    assert bar.pos() == coordinator.anchor_point()

    bar.move(QPoint(700, 900))  # not parked: closing displays leaves it alone
    coordinator.add_multi_window(tiles[0])
    coordinator.close_multi_windows()
    assert bar.pos() == QPoint(700, 900)
    for widget in (bar, anchor):
        widget.close()
    app.processEvents()
