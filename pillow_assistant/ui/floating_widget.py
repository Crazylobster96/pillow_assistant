"""Composition shell for the floating Pillow Assistant icon."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.event_presenter import EventPresenter
from pillow_assistant.ui.gesture_controller import (
    DRAG_THRESHOLD_PX,
    LONG_PRESS_MS,
    GestureController,
    GestureState,
)
from pillow_assistant.ui.icon_assets import (
    create_close_icon,
    create_keyboard_icon,
    create_microphone_icon,
    create_pillow_icon,
    is_supported_image,
)
from pillow_assistant.ui.voice_controller import NO_ASR, VoiceController
from pillow_assistant.ui.window_coordinator import WindowCoordinator
from storage import Storage

__all__ = [
    "DRAGGING_SELF",
    "DRAG_THRESHOLD_PX",
    "FloatingAssistant",
    "IDLE",
    "LONG_PRESS_MS",
    "PRESSED",
    "RECORDING",
    "create_close_icon",
    "create_keyboard_icon",
    "create_microphone_icon",
    "create_pillow_icon",
    "is_supported_image",
]

IDLE = GestureState.IDLE.value
PRESSED = GestureState.PRESSED.value
RECORDING = GestureState.RECORDING.value
DRAGGING_SELF = "dragging_self"


class FloatingAssistant(QWidget):
    """Always-on-top icon that composes gesture, voice, event, and window controllers."""

    # Keep the original public signals as a compatibility facade while the
    # worker implementation lives in VoiceController.
    transcribed = Signal(str)
    voice_saved = Signal(object)

    def __init__(self, storage: Storage, bus=None, session=None, vault=None,
                 project_store=None, undo_manager=None, ask_broker=None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.bus = bus
        self.session = session
        self.vault = vault
        self.project_store = project_store
        self.undo_manager = undo_manager
        self.ask_broker = ask_broker

        flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        self.setWindowFlags(flags)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self._build_ui()

        self.windows = WindowCoordinator(
            self,
            storage,
            bus=bus,
            session=session,
            vault=vault,
            project_store=project_store,
        )
        self.presenter = EventPresenter(
            self.windows,
            storage=storage,
            bus=bus,
            session=session,
            undo_manager=undo_manager,
            ask_broker=ask_broker,
            flash_icon=self._flash_icon,
        )

        recordings_dir = Path(storage.db_path).parent / "recordings"
        self.voice = VoiceController(recordings_dir, parent=self)
        self.voice.recording_saved.connect(self.voice_saved.emit)
        self.voice.transcribed.connect(self.transcribed.emit)
        self.voice_saved.connect(self._on_voice_saved)
        self.transcribed.connect(self._on_transcribed)

        self.gestures = GestureController(self.pillow_button, self, parent=self)
        self.gestures.clicked.connect(self._on_clicked)
        self.gestures.right_clicked.connect(self.windows.open_radial_menu)
        self.gestures.recording_started.connect(self._on_recording_started)
        self.gestures.recording_finished.connect(self._on_recording_finished)
        self.gestures.drag_started.connect(self.windows.capture_follow_offsets)
        self.gestures.move_requested.connect(self.move)
        self.gestures.drag_finished.connect(self.windows.finish_anchor_drag)

        if bus is not None:
            bus.event.connect(self.presenter.handle)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.pillow_button = QPushButton(self)
        self.pillow_button.setIcon(QIcon(create_pillow_icon(96)))
        self.pillow_button.setIconSize(QSize(72, 72))
        self.pillow_button.setFixedSize(96, 96)
        self.pillow_button.setFlat(True)
        self.pillow_button.setCursor(Qt.PointingHandCursor)
        self.pillow_button.setToolTip(t("icon.tooltip"))
        self._apply_idle_style()
        layout.addWidget(self.pillow_button)

    def _apply_idle_style(self) -> None:
        self.pillow_button.setStyleSheet(
            """
            QPushButton { border: none; background: transparent; border-radius: 38px; }
            QPushButton:hover { background: rgba(255,255,255,45); border-radius: 38px; }
            """
        )

    def _apply_recording_style(self) -> None:
        self.pillow_button.setStyleSheet(
            """
            QPushButton { border: 3px solid rgba(235,90,110,235); background: rgba(255,210,215,170);
                border-radius: 38px; }
            """
        )

    def _flash_icon(self) -> None:
        self.pillow_button.setStyleSheet(
            "QPushButton { border: 3px solid rgba(120,200,140,235); "
            "background: rgba(210,255,220,150); border-radius: 38px; }"
        )
        QTimer.singleShot(600, self._apply_idle_style)

    def _on_clicked(self) -> None:
        self.windows.close_radial()
        if self.windows.quick_is_open():
            self.windows.close_displays()
        else:
            self.windows.open_quick_input()

    def _on_recording_started(self) -> None:
        self.windows.close_radial()
        self._apply_recording_style()
        self.voice.start()

    def _on_recording_finished(self) -> None:
        self._apply_idle_style()
        self.voice.stop()

    def _on_voice_saved(self, path) -> None:
        bar = self.windows.open_quick_input()
        if bar is None:
            return
        if not path:
            bar.prompt_edit.setPlaceholderText(t("voice.none"))
            return
        bar.prompt_edit.setPlaceholderText(t("voice.transcribing"))
        self.voice.transcribe(path)

    def _on_transcribed(self, text: str) -> None:
        bar = self.windows.quick
        if bar is None:
            return
        if text == NO_ASR:
            bar.prompt_edit.setPlaceholderText(t("voice.no_asr"))
        elif text:
            bar.prompt_edit.setText(text)
            bar.prompt_edit.setFocus()
        else:
            bar.prompt_edit.setPlaceholderText(t("voice.failed"))

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        if hasattr(self, "windows"):
            self.windows.follow_anchor_move(self.pos())

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        if paths:
            self.windows.open_file_panel(paths)
