from __future__ import annotations

import datetime
import queue
import threading
import wave
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

try:
    import numpy as np
    import sounddevice as sd

    HAVE_AUDIO = True
except ImportError:  # pragma: no cover
    HAVE_AUDIO = False

from storage import Storage


class AudioRecorderDialog(QDialog):
    """Minimal audio recorder dialog to capture microphone input."""

    def __init__(self, storage: Storage, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("语音输入采集")
        self.setModal(True)

        self.status_label = QLabel(self)
        self.status_label.setText("点击开始录音。录音完成后会保存为 WAV 文件。")

        self.start_button = QPushButton("开始录音", self)
        self.stop_button = QPushButton("停止并保存", self)
        self.stop_button.setEnabled(False)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(buttons)

        self.stream: Optional["sd.InputStream"] = None
        self.frame_queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self.audio_timer = QTimer(self)
        self.audio_timer.timeout.connect(self._drain_queue)
        self.audio_chunks: list["np.ndarray"] = []
        self.capture_thread: Optional[threading.Thread] = None
        self.is_recording = False

        self.start_button.clicked.connect(self._start_recording)
        self.stop_button.clicked.connect(self._stop_recording)

        if not HAVE_AUDIO:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.status_label.setText(
                "声音采集依赖 numpy 与 sounddevice，请先安装：\n"
                "pip install numpy sounddevice"
            )

    def _start_recording(self) -> None:
        if not HAVE_AUDIO or self.is_recording:
            return

        try:
            self.stream = sd.InputStream(
                samplerate=44100,
                channels=1,
                dtype="int16",
                callback=self._audio_callback,
            )
            self.stream.start()
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "录音失败", f"无法初始化录音设备：\n{exc}")
            return

        self.audio_chunks.clear()
        self.is_recording = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText("正在录音... 点击“停止并保存”结束。")
        self.audio_timer.start(200)

    def _stop_recording(self) -> None:
        if not HAVE_AUDIO or not self.is_recording:
            return

        self.audio_timer.stop()
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        finally:
            self.stream = None

        self._drain_queue()
        self.is_recording = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        if not self.audio_chunks:
            self.status_label.setText("没有捕获到音频数据。")
            return

        audio = np.concatenate(self.audio_chunks, axis=0)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_dir = Path(self.storage.db_path).parent / "recordings"
        audio_dir.mkdir(parents=True, exist_ok=True)
        output_path = audio_dir / f"recording_{timestamp}.wav"
        self._write_wav(output_path, audio)

        self.status_label.setText(f"录音保存至：{output_path}")

    def _audio_callback(self, indata, frames, time, status) -> None:  # pragma: no cover
        if status:
            # Status includes XRuns etc. We append message for debugging.
            self.status_label.setText(f"录音状态: {status}")
        self.frame_queue.put(indata.copy())

    def _drain_queue(self) -> None:
        if not HAVE_AUDIO:
            return
        while not self.frame_queue.empty():
            self.audio_chunks.append(self.frame_queue.get_nowait())

    def _write_wav(self, path: Path, audio: "np.ndarray") -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # int16
            wav_file.setframerate(44100)
            wav_file.writeframes(audio.tobytes())

