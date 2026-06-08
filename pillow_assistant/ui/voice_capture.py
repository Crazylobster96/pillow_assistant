"""Press-to-talk voice capture for the FSM long-press gesture.

Records while the icon is held and saves a WAV on release. Transcription (ASR)
is intentionally deferred to R1 — this component only captures audio so the
long-press gesture is functional today; ``on_finished`` receives the saved WAV
path (or ``None`` if capture was unavailable / empty).
"""

from __future__ import annotations

import datetime
import queue
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

try:
    import numpy as np
    import sounddevice as sd

    HAVE_AUDIO = True
except ImportError:  # pragma: no cover
    HAVE_AUDIO = False

SAMPLE_RATE = 44100


class VoiceCapture:
    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self._stream = None
        self._queue: "queue.Queue" = queue.Queue()
        self._chunks: list = []
        self._recording = False
        # start() and stop() may run on different worker threads; serialize them
        # so a slow device-open can't race the stop into an orphaned stream.
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return HAVE_AUDIO

    @property
    def recording(self) -> bool:
        return self._recording

    def start(self) -> bool:
        with self._lock:
            if not HAVE_AUDIO or self._recording:
                return False
            self._chunks.clear()
            # Discard any stale frames left in the queue from a prior capture.
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=self._callback
                )
                self._stream.start()
            except Exception:  # pragma: no cover - device errors
                self._stream = None
                return False
            self._recording = True
            return True

    def stop(self, on_finished: Optional[Callable[[Optional[str]], None]] = None) -> Optional[str]:
        with self._lock:
            if not HAVE_AUDIO or not self._recording:
                if on_finished:
                    on_finished(None)
                return None
            try:
                if self._stream:
                    self._stream.stop()
                    self._stream.close()
            finally:
                self._stream = None
            self._recording = False
            self._drain()
            if not self._chunks:
                if on_finished:
                    on_finished(None)
                return None

            audio = np.concatenate(self._chunks, axis=0)
            self._chunks = []
            self.output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.output_dir / f"voice_{timestamp}.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(audio.tobytes())
        if on_finished:
            on_finished(str(path))
        return str(path)

    def _callback(self, indata, frames, time, status) -> None:  # pragma: no cover
        self._queue.put(indata.copy())

    def _drain(self) -> None:
        while not self._queue.empty():
            self._chunks.append(self._queue.get_nowait())
