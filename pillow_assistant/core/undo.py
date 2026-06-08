"""5-second undo (R3): hold reversible high-risk actions briefly so the user can
undo without a blocking confirm dialog. Entries expire after the TTL.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable

TTL = 5.0


@dataclass
class _Entry:
    token: str
    label: str
    fn: Callable[[], None]
    ts: float


class UndoManager:
    def __init__(self, ttl: float = TTL) -> None:
        self.ttl = ttl
        self._pending: dict[str, _Entry] = {}

    def register(self, label: str, fn: Callable[[], None]) -> str:
        self._expire()
        token = uuid.uuid4().hex[:8]
        self._pending[token] = _Entry(token, label, fn, time.time())
        return token

    def undo(self, token: str) -> bool:
        self._expire()
        entry = self._pending.pop(token, None)
        if entry is None:
            return False
        try:
            entry.fn()
            return True
        except Exception:
            return False

    def pending(self) -> list[str]:
        self._expire()
        return list(self._pending)

    def _expire(self) -> None:
        now = time.time()
        for token in [t for t, e in self._pending.items() if now - e.ts > self.ttl]:
            self._pending.pop(token, None)
