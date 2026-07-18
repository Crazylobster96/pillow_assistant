"""Threaded recording and speech-to-text controller for the floating UI."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from pillow_assistant.core import asr
from pillow_assistant.ui.voice_capture import VoiceCapture

NO_ASR = "__NO_ASR__"


class VoiceController(QObject):
    recording_saved = Signal(object)
    transcribed = Signal(str)

    def __init__(self, recordings_dir: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._capture = VoiceCapture(Path(recordings_dir))
        threading.Thread(target=asr.warmup, name="pillow-asr-warmup", daemon=True).start()

    def start(self) -> None:
        threading.Thread(target=self._start_capture, name="pillow-rec-start", daemon=True).start()

    def _start_capture(self) -> None:
        try:
            self._capture.start()
        except Exception:
            pass

    def stop(self) -> None:
        threading.Thread(target=self._stop_capture, name="pillow-rec-stop", daemon=True).start()

    def _stop_capture(self) -> None:
        try:
            path = self._capture.stop()
        except Exception:
            path = None
        self.recording_saved.emit(path)

    def transcribe(self, path: str | Path) -> None:
        threading.Thread(
            target=self._transcribe,
            args=(path,),
            name="pillow-asr",
            daemon=True,
        ).start()

    def _transcribe(self, path: str | Path) -> None:
        try:
            if not asr.available():
                self.transcribed.emit(NO_ASR)
                return
            text = asr.transcribe(path)
        except Exception:
            text = ""
        self.transcribed.emit(text)
