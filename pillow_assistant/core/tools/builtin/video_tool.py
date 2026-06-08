"""process_video tool: probe / split / compress / extract frames via ffmpeg.

Lets the Agent adapt a dragged-in video to the backend model's limits: probe
duration & size, split into time segments, compress to a target size, or
extract evenly-spaced frames (for vision models that only take images).
Outputs land in the project workspace as artifacts. Degrades gracefully when
ffmpeg is missing (system ffmpeg first, then the optional imageio-ffmpeg
bundled binary).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.builtin.file_tool import _is_reference, _under

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".mpg", ".mpeg"}
RUN_TIMEOUT = 600  # transcodes can be slow
AUDIO_KBPS = 96


def find_ffmpeg() -> Optional[str]:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:  # optional pip package that ships an ffmpeg binary
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def _fps(rate: Optional[str]) -> Optional[float]:
    try:
        num, _, den = (rate or "").partition("/")
        d = float(den or 1)
        return round(float(num) / d, 2) if d else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path: str | Path, ffprobe: Optional[str] = None) -> dict:
    """Duration / size / resolution / codec for a media file."""
    p = Path(path)
    info: dict = {"size_mb": round(p.stat().st_size / 1048576, 2)}
    ffprobe = ffprobe or find_ffprobe()
    if not ffprobe:
        return info
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        return info
    fmt = data.get("format", {})
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        info["duration"] = round(float(fmt.get("duration") or 0), 2)
    except (TypeError, ValueError):
        pass
    info.update({k: v.get(k2) for k, k2 in
                 (("width", "width"), ("height", "height"), ("codec", "codec_name"))})
    info["fps"] = _fps(v.get("avg_frame_rate"))
    return info


def target_bitrate_kbps(max_mb: float, seconds: float) -> int:
    """Video bitrate (kbps) so video+audio fits max_mb (pure, testable)."""
    total_kbits = max(0.5, float(max_mb)) * 8192
    kbps = total_kbits / max(float(seconds), 0.1) - AUDIO_KBPS
    return max(80, int(kbps * 0.95))  # 5% container overhead margin


def split_seconds_for(max_mb: float, duration: float, size_mb: float) -> int:
    """Segment length so each stream-copied piece is roughly under max_mb."""
    if size_mb <= 0 or duration <= 0:
        return 60
    return max(5, int(duration * (float(max_mb) / size_mb) * 0.9))


class ProcessVideoTool:
    name = "process_video"
    permission = Permission.WRITE_WS
    description = t("tool.vid.desc")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": t("tool.vid.path")},
            "action": {"type": "string", "enum": ["probe", "split", "compress", "frames"],
                       "description": t("tool.vid.action")},
            "segment_seconds": {"type": "integer", "description": t("tool.vid.segment_seconds")},
            "max_mb": {"type": "number", "description": t("tool.vid.max_mb")},
            "max_height": {"type": "integer", "description": t("tool.vid.max_height")},
            "frame_count": {"type": "integer", "description": t("tool.vid.frame_count")},
        },
        "required": ["path", "action"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw = (args.get("path") or "").strip()
        action = (args.get("action") or "probe").strip().lower()
        ws = Path(ctx.workspace)
        p = (ws / raw) if not Path(raw).is_absolute() else Path(raw)
        if not (_under(ws, p) or _is_reference(ctx, p)):
            return ToolResult(ok=False, text=t("tool.vid.denied", path=raw))
        if not p.is_file():
            return ToolResult(ok=False, text=t("tool.vid.not_found", path=raw))
        ffmpeg = find_ffmpeg()
        if ffmpeg is None and action != "probe":
            return ToolResult(ok=False, text=t("tool.vid.no_ffmpeg"))

        loop = asyncio.get_event_loop()
        try:
            if action == "probe":
                info = await loop.run_in_executor(None, lambda: probe(p))
                return ToolResult(ok=True, text=t("tool.vid.probe_result", info=json.dumps(info, ensure_ascii=False)))
            info = await loop.run_in_executor(None, lambda: probe(p))
            duration = float(info.get("duration") or 0)
            if action == "split":
                seg = int(args.get("segment_seconds") or 0)
                if seg <= 0 and args.get("max_mb"):
                    seg = split_seconds_for(float(args["max_mb"]), duration, info.get("size_mb") or 0)
                seg = seg or 60
                pattern = ws / f"{p.stem}_part%03d{p.suffix if p.suffix.lower() == '.mp4' else '.mp4'}"
                cmd = [ffmpeg, "-y", "-i", str(p), "-c", "copy", "-map", "0",
                       "-f", "segment", "-segment_time", str(seg),
                       "-reset_timestamps", "1", str(pattern)]
                await loop.run_in_executor(None, lambda: subprocess.run(
                    cmd, capture_output=True, timeout=RUN_TIMEOUT, check=True))
                outs = sorted(ws.glob(f"{p.stem}_part*"))
                arts = [o.name for o in outs]
                return ToolResult(ok=True, text=t("tool.vid.split_done", n=len(arts), seg=seg,
                                                  files=", ".join(arts[:12])), artifacts=arts)
            if action == "compress":
                max_mb = float(args.get("max_mb") or 0)
                if max_mb <= 0:
                    return ToolResult(ok=False, text=t("tool.vid.need_max_mb"))
                if duration <= 0:
                    return ToolResult(ok=False, text=t("tool.vid.no_duration"))
                kbps = target_bitrate_kbps(max_mb, duration)
                max_h = int(args.get("max_height") or 720)
                out = ws / f"{p.stem}_compressed.mp4"
                cmd = [ffmpeg, "-y", "-i", str(p),
                       "-vf", f"scale=-2:'min({max_h},ih)'",
                       "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                       "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", str(out)]
                await loop.run_in_executor(None, lambda: subprocess.run(
                    cmd, capture_output=True, timeout=RUN_TIMEOUT, check=True))
                new_mb = round(out.stat().st_size / 1048576, 2)
                return ToolResult(ok=True, text=t("tool.vid.compress_done", name=out.name,
                                                  mb=new_mb, kbps=kbps), artifacts=[out.name])
            if action == "frames":
                n = max(1, int(args.get("frame_count") or 8))
                if duration <= 0:
                    return ToolResult(ok=False, text=t("tool.vid.no_duration"))
                fps = n / duration
                pattern = ws / f"{p.stem}_frame%02d.jpg"
                cmd = [ffmpeg, "-y", "-i", str(p), "-vf", f"fps={fps:.6f}",
                       "-frames:v", str(n), "-q:v", "3", str(pattern)]
                await loop.run_in_executor(None, lambda: subprocess.run(
                    cmd, capture_output=True, timeout=RUN_TIMEOUT, check=True))
                outs = sorted(ws.glob(f"{p.stem}_frame*.jpg"))
                arts = [o.name for o in outs]
                return ToolResult(ok=True, text=t("tool.vid.frames_done", n=len(arts),
                                                  files=", ".join(arts)), artifacts=arts)
            return ToolResult(ok=False, text=t("tool.vid.bad_action", action=action))
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"")[-400:].decode("utf-8", "replace")
            return ToolResult(ok=False, text=t("tool.vid.failed", err=err))
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, text=t("tool.vid.timeout", n=RUN_TIMEOUT))
