"""Local-only LLM I/O logger.

Records every model call (prompt, response, usage, latency, errors) to a
rolling JSONL file under the user's data directory. Strictly local — never
uploaded, never shared. The point is dev/ops observability: when a UI bug
silently swallows a streamed response (as just happened with the PySide6
``QTextCursor.End`` crash), the log shows that the model *did* reply, so the
investigation lands on the UI immediately instead of bouncing between layers.

Design constraints (matches v1.7 §3.13.8 / module doc M-Observability):

* JSON-lines format: one record per call, grep / ``jq`` friendly.
* Rolling by day; files older than ``KEEP_DAYS`` are pruned on import.
* Single-day size cap (``MAX_BYTES_PER_DAY``); after the cap further calls are
  silently dropped to avoid runaway disk use during a hot loop.
* A failure to write the log MUST NOT raise — observability outages must
  never break the product.
* Toggle ``ENABLED`` (or env var ``PILLOW_LLM_LOG=0``) to opt out entirely.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback as _tb
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


# ------------------------------- Configuration -------------------------------

KEEP_DAYS: int = 7
MAX_BYTES_PER_DAY: int = 100 * 1024 * 1024  # 100 MB
ENABLED: bool = os.environ.get("PILLOW_LLM_LOG", "1") not in ("0", "false", "False")

_lock = threading.Lock()


def _log_dir() -> Path:
    """Return the platform-appropriate logs directory; create it if missing."""
    # Project convention: the running app uses ``<repo>/data`` for SQLite +
    # secrets, so co-locating logs there keeps everything under one privacy
    # boundary the user already understands. On Windows the same path is used.
    base = Path(__file__).resolve().parents[2] / "data" / "logs"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fallback to user home if the package directory isn't writable.
        base = Path.home() / ".pillow" / "logs"
        base.mkdir(parents=True, exist_ok=True)
    return base


def _log_file_for(day: datetime) -> Path:
    return _log_dir() / f"llm-{day:%Y%m%d}.jsonl"


def _today_file() -> Path:
    return _log_file_for(datetime.now())


# ------------------------------- Retention -----------------------------------

def cleanup_old_logs(keep_days: int = KEEP_DAYS) -> int:
    """Delete daily log files older than ``keep_days``. Returns count removed."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    try:
        for path in _log_dir().glob("llm-*.jsonl"):
            try:
                day = datetime.strptime(path.stem.removeprefix("llm-"), "%Y%m%d")
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


# Best-effort cleanup at import time — cheap, runs once per process.
cleanup_old_logs()


# ------------------------------- Writer --------------------------------------

def _write(record: dict) -> None:
    """Append a JSONL record. Never raises — observability is best-effort."""
    if not ENABLED:
        return
    try:
        path = _today_file()
        try:
            if path.exists() and path.stat().st_size > MAX_BYTES_PER_DAY:
                # Daily budget exhausted — drop further records silently.
                return
        except OSError:
            pass
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        # Logging must not break the app. Swallow everything.
        pass


# ------------------------------- Public API ----------------------------------

@contextmanager
def log_llm_call(
    *,
    provider: str,
    model: str,
    prompt: str,
    api_base: str | None = None,
    image_count: int = 0,
    extra: dict[str, Any] | None = None,
) -> Iterator[dict]:
    """Context manager wrapping one LLM call. Yields a mutable holder.

    Caller is expected to populate ``holder["response"]`` with the aggregated
    response text (and optionally ``holder["usage"]`` / ``holder["tool_calls"]``)
    before exiting the block. If the block raises, the exception is logged with
    any partial response collected so far and then re-raised.

    Note: ``prompt`` is truncated to ~8 KB in the on-disk record; full content
    is fine to pass in but huge prompts (images already encoded as data URLs,
    multi-MB pasted text) shouldn't bloat the log indefinitely.
    """
    req_id = uuid.uuid4().hex[:12]
    t0 = time.time()
    record: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "request_id": req_id,
        "provider": provider,
        "model": model,
        "api_base": api_base,
        "image_count": image_count,
        "prompt": (prompt or "")[:8000],
        "prompt_truncated": len(prompt or "") > 8000,
        "extra_keys": sorted(extra.keys()) if extra else [],
    }
    holder: dict[str, Any] = {}
    try:
        yield holder
    except Exception as e:
        record.update({
            "ok": False,
            "duration_ms": int((time.time() - t0) * 1000),
            "error_type": type(e).__name__,
            "error_msg": str(e),
            "traceback": _tb.format_exc(),
            "partial_response": (holder.get("response") or "")[:8000],
        })
        _write(record)
        raise
    else:
        response = holder.get("response") or ""
        record.update({
            "ok": True,
            "duration_ms": int((time.time() - t0) * 1000),
            "response": response[:8000],
            "response_truncated": len(response) > 8000,
            "response_len": len(response),
            "usage": holder.get("usage"),
            "tool_calls": holder.get("tool_calls"),
        })
        _write(record)
