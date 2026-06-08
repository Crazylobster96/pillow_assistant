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
        row.addWidget(self._slider, 1)

        self._time_label = QLabel("00:00 / 00:00", self)
        row.addWidget(self._time_label)
        layout.addLayout(row)

        self._player.durationChanged.connect(self._on_duration)
        self._player.positionChanged.connect(self._on_position)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)
        self._err_label = QLabel("", self)
        self._err_label.setWordWrap(True)
        self._err_label.setStyleSheet("color:#f0a0a0; font-size:12px;")
        self._err_label.hide()
        layout.addWidget(self._err_label)
        return True

    def _toggle_play(self) -> None:
        p = getattr(self, "_player", None)
        if p is None:
            return
        if p.playbackState() == QMediaPlayer.PlayingState:
            p.pause()
        else:
            p.play()

    def _on_state(self, state) -> None:
        self._play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_duration(self, ms: int) -> None:
        self._slider.setRange(0, int(ms))
        self._on_position(self._player.position())

    def _on_position(self, ms: int) -> None:
        if not self._slider.isSliderDown():
            self._slider.setValue(int(ms))
        self._time_label.setText(f"{_fmt_ms(ms)} / {_fmt_ms(self._player.duration())}")

    def _on_error(self, _error, error_string: str = "") -> None:
        self._err_label.setText(t("panel.video_play_failed", err=error_string or _error))
        self._err_label.show()

    def closeEvent(self, event) -> None:  # noqa: N802
        p = getattr(self, "_player", None)
        if p is not None:
            try:
                p.stop()
                p.setSource(QUrl())  # release the file handle
            except Exception:
                pass
        super().closeEvent(event)

    # -- fallback: ffmpeg first-frame thumbnail -------------------------------
    def _build_thumbnail(self, layout: QVBoxLayout, path: Path) -> None:
        if HAVE_MM is False:
            layout.addWidget(QLabel(t("panel.video_need_mm"), self))
        shown = False
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            try:
                tmp = Path(tempfile.mkdtemp(prefix="pillow_thumb_")) / "thumb.jpg"
                subprocess.run([ffmpeg, "-y", "-ss", "1", "-i", str(path),
                                "-frames:v", "1", "-q:v", "4", str(tmp)],
                               capture_output=True, timeout=30, check=True)
                pix = QPixmap(str(tmp))
                if not pix.isNull():
                    self._orig = pix
                    self._img_label = QLabel(self)
                    self._img_label.setAlignment(Qt.AlignCenter)
                    self._img_label.setMinimumSize(1, 1)
                    self._img_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                    layout.addWidget(self._img_label)
                    shown = True
            except Exception:
                pass
        if not shown:
            layout.addWidget(QLabel(t("panel.video_need_ffmpeg"), self))
            layout.addStretch(1)

    def _on_resized(self) -> None:
        orig = getattr(self, "_orig", None)
        label = getattr(self, "_img_label", None)
        if orig is None or label is None or orig.isNull():
            return
        size = label.size()
        if size.width() > 4 and size.height() > 4:
            label.setPixmap(orig.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def initial_size(self) -> tuple[int, int]:
        return (720, 620)
