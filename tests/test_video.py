"""Tests for process_video: pure helpers always; ffmpeg paths only if present."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.video_tool import (
    ProcessVideoTool,
    probe,
    split_seconds_for,
    target_bitrate_kbps,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def test_target_bitrate():
    assert target_bitrate_kbps(1, 25) > 80
    assert target_bitrate_kbps(10, 60) > target_bitrate_kbps(1, 60)
    assert target_bitrate_kbps(0.0001, 9999) == 80  # floor


def test_split_seconds_for():
    assert 5 <= split_seconds_for(0.2, 25, 0.67) <= 25
    assert split_seconds_for(10, 0, 0) == 60      # unknown duration -> default
    assert split_seconds_for(0.001, 100, 100) == 5  # floor


def test_denied_and_bad_action(tmp_path):
    ctx = ToolContext(workspace=tmp_path)
    tool = ProcessVideoTool()
    r = asyncio.run(tool({"path": "/etc/passwd", "action": "probe"}, ctx))
    assert not r.ok
    r = asyncio.run(tool({"path": "nope.mp4", "action": "probe"}, ctx))
    assert not r.ok


@pytest.fixture
def sample_video(tmp_path):
    out = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", str(out)],
        capture_output=True, timeout=120, check=True,
    )
    return out


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_probe_and_actions(tmp_path, sample_video):
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(workspace=ws, references=[str(sample_video)])
    tool = ProcessVideoTool()
    run = lambda a: asyncio.run(tool(a, ctx))

    info = probe(sample_video)
    assert 11 <= info["duration"] <= 13 and info["width"] == 320

    r = run({"path": str(sample_video), "action": "split", "segment_seconds": 5})
    assert r.ok and len(r.artifacts) >= 2

    r = run({"path": str(sample_video), "action": "compress", "max_mb": 0.3, "max_height": 120})
    assert r.ok
    out = ws / r.artifacts[0]
    assert out.stat().st_size / 1048576 <= 0.4
    assert probe(out)["height"] <= 120

    r = run({"path": str(sample_video), "action": "frames", "frame_count": 4})
    assert r.ok and 3 <= len(r.artifacts) <= 4
