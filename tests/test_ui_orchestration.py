"""Component-boundary tests for the floating assistant UI."""

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject

from pillow_assistant.contracts import EventType
from pillow_assistant.ui.event_presenter import EventPresenter
from pillow_assistant.ui.floating_widget import (
    DRAGGING_SELF,
    IDLE,
    PRESSED,
    RECORDING,
    create_close_icon,
    is_supported_image,
)
from pillow_assistant.ui.gesture_controller import GestureController, GestureState
from pillow_assistant.ui.window_coordinator import WindowCoordinator


class _Window:
    def __init__(self, *, visible=True) -> None:
        self.visible = visible
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible


def _coordinator() -> WindowCoordinator:
    return WindowCoordinator(anchor=object(), storage=object())


def test_stale_destroyed_window_cannot_clear_new_slot() -> None:
    coordinator = _coordinator()
    previous = _Window()
    current = _Window()

    coordinator.set_window("quick", previous)
    coordinator.set_window("quick", current)
    coordinator.clear_window("quick", previous)

    assert coordinator.quick is current


def test_close_displays_closes_only_display_windows() -> None:
    coordinator = _coordinator()
    displays = {name: _Window() for name in ("quick", "panel", "surface")}
    radial = _Window()
    multi = _Window()
    for name, window in displays.items():
        coordinator.set_window(name, window)
    coordinator.set_window("radial", radial)
    coordinator.add_multi_window(multi)

    coordinator.close_displays()

    assert all(window.closed for window in displays.values())
    assert multi.closed
    assert not radial.closed
    assert coordinator.window("radial") is radial


def test_event_presenter_routes_agent_events() -> None:
    class RecordingPresenter(EventPresenter):
        def __init__(self) -> None:
            super().__init__(_coordinator(), storage=object())
            self.calls = []

        def show_undo(self, event) -> None:
            self.calls.append("undo")

        def show_surface(self, event) -> None:
            self.calls.append("surface")

        def show_ask(self, event) -> None:
            self.calls.append("ask")

    presenter = RecordingPresenter()
    for event_type in (EventType.UNDO, EventType.SURFACE, EventType.ASK):
        presenter.handle(SimpleNamespace(type=event_type))

    assert presenter.calls == ["undo", "surface", "ask"]


def test_long_press_transition_is_owned_by_gesture_controller() -> None:
    target = QObject()
    anchor = QObject()
    controller = GestureController(target, anchor)
    started = []
    controller.recording_started.connect(lambda: started.append(True))

    controller.state = GestureState.PRESSED
    controller._begin_recording()

    assert controller.state is GestureState.RECORDING
    assert started == [True]


def test_floating_widget_keeps_legacy_public_helpers() -> None:
    assert (IDLE, PRESSED, RECORDING, DRAGGING_SELF) == (
        "idle",
        "pressed",
        "recording",
        "dragging_self",
    )
    assert callable(create_close_icon)
    assert is_supported_image("preview.WEBP")
    assert not is_supported_image("notes.txt")
