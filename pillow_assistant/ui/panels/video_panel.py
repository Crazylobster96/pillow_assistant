"""Video preview panel: real playback via QtMultimedia (play/pause, seek bar,
time display); degrades to an ffmpeg first-frame thumbnail, then to a plain
info card when components are missing."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.builtin.video_tool import find_ffmpeg, probe
from pillow_assistant.ui.panels.base_panel import FilePanel

try:  # PySide6-Addons (part of the full PySide6 pip package)
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    HAVE_MM = True
except ImportError:  # pragma: no cover
    HAVE_MM = False


def _fmt_ms(ms) -> str:
    s = max(0, int(ms or 0) // 1000)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{(s % 3600) // 60:02d}:{s % 60:02d}"


def _fmt_duration(seconds) -> str:
    try:
        return _fmt_ms(float(seconds) * 1000)
    except (TypeError, ValueError):
        return "?"


class VideoPanel(FilePanel):
    TITLE = t("panel.video")

    def build_preview(self, layout: QVBoxLayout) -> None:
        path = Path(self.paths[0])
        info: dict = {}
        try:
            info = probe(path)
        except Exception:
            pass

        meta = f"{path.name} · {info.get('size_mb', '?')} MB"
        if info.get("duration"):
            meta += f" · {_fmt_duration(info['duration'])}"
            if info.get("width"):
                meta += f" · {info.get('width')}x{info.get('height')}"
        meta_label = QLabel(meta, self)
        meta_label.setWordWrap(True)
        layout.addWidget(meta_label)

        if HAVE_MM and self._build_player(layout, path):
            return
        self._build_thumbnail(layout, path)

    # -- playback (QtMultimedia) ---------------------------------------------
    def _build_player(self, layout: QVBoxLayout, path: Path) -> bool:
        try:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._audio.setVolume(0.8)
            self._player.setAudioOutput(self._audio)
            video = QVideoWidget(self)
            video.setMinimumSize(200, 160)
            video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self._player.setVideoOutput(video)
            self._player.setSource(QUrl.fromLocalFile(str(path)))
        except Exception:
            return False
        layout.addWidget(video, 1)

        row = QHBoxLayout()
        self._play_btn = QPushButton("▶", self)
        self._play_btn.setFixedSize(34, 28)
        self._play_btn.clicked.connect(self._toggle_play)
        row.addWidget(self._play_btn)

        self._slider = QSlider(Qt.Horizontal, self)
        self._slider.setRange(0, 0)
        self._slider.sliderMoved.connect(self._player.setPosition)
        row.addW