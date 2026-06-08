"""Ask broker: lets the Agent (background asyncio loop) ask the user a question
through the UI (Qt main thread) and await the answer.

The tool calls ``await broker.ask(...)`` on the bus loop; that emits an ASK event
the UI turns into a dialog. When the user answers, the UI calls
``broker.resolve(ask_id, answer)`` from the main thread, which completes the
awaiting future thread-safely.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from pillow_assistant.contracts import AgentEvent, EventType

ASK_TIMEOUT_S = 300  # don't block an agent run forever waiting on the user


class AskBroker:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}

    async def ask(self, emit, request_id: str, spec: dict) -> dict:
        """Emit an ASK event and await the user's answer.

        Returns ``{"answer": str, "cancelled": bool, "timeout": bool}``.
        """
        loop = asyncio.get_event_loop()
        ask_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = loop.create_future()
        self._pending[ask_id] = (loop, fut)
        meta = {"ask_id": ask_id}
        meta.update(spec or {})
        try:
            await emit(AgentEvent(request_id=request_id, type=EventType.ASK, meta=meta))
            return await asyncio.wait_for(fut, timeout=ASK_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"answer": "", "cancelled": True, "timeout": True}
        finally:
            self._pending.pop(ask_id, None)

    def resolve(self, ask_id: str, answer: dict) -> None:
        """Complete a pending ask. Safe to call from the Qt main thread."""
        item = self._pending.get(ask_id)
        if item is None:
            return
        loop, fut = item
        if not fut.done():
            loop.call_soon_threadsafe(fut.set_result, answer)

    def cancel_all(self) -> None:
        for ask_id in list(self._pending):
            self.resolve(ask_id, {"answer": "", "cancelled": True})
