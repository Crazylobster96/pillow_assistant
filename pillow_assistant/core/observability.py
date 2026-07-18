"""Lightweight audit log (T3 / NFR-14): record each run and tool call as JSONL,
so an Agent run is traceable for debugging. Per-project (audit.jsonl) or chat.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pillow_assistant.core.tools.permission_policy import redact_sensitive


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _write(self, rec: dict) -> None:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def run_start(self, prompt: str) -> None:
        self._write({"kind": "run_start", "prompt": (prompt or "")[:200]})

    def run_end(self, final_len: int) -> None:
        self._write({"kind": "run_end", "final_len": final_len})

    def tool_call(self, name: str, args: dict, ok, ms: int, result_len: int) -> None:
        safe_args = redact_sensitive(args)
        self._write({"kind": "tool", "name": name, "ok": ok, "ms": ms,
                     "result_len": result_len, "args": str(safe_args)[:200]})
