"""The question box grows with the text up to five lines, then scrolls."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from pillow_assistant.ui.prompt_edit import MAX_LINES, PromptEdit  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _box(app):
    box = PromptEdit()
    box.resize(400, 40)
    box.show()
    app.processEvents()
    return box


def test_it_grows_to_five_lines_then_scrolls(app):
    box = _box(app)
    one = box.height()
    box.setText("a\nb\nc")
    app.processEvents()
    three = box.height()
    box.setText("\n".join("line" for _ in range(12)))
    app.processEvents()
    many = box.height()
    box.setText("\n".join("line" for _ in range(MAX_LINES)))
    app.processEvents()
    five = box.height()
    assert one < three < five and many == five
    box.setText("\n".join("line" for _ in range(12)))
    app.processEvents()
    assert box.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded


def test_enter_sends_and_shift_enter_breaks_the_line(app):
    box = _box(app)
    sent = []
    box.returnPressed.connect(lambda: sent.append(box.text()))
    box.setText("hello")
    QtWidgets.QApplication.sendEvent(box, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ShiftModifier, "\r"))
    assert box.text() == "hello\n" and not sent
    QtWidgets.QApplication.sendEvent(box, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))
    assert sent == ["hello\n"]
