"""Event bus bridging the Qt UI thread and a background asyncio loop.

The UI calls :meth:`EventBus.submit` from the Qt main thread. The request is
processed on a dedicated asyncio loop running in its own thread, so model calls
never block the UI (NFR-1). Resulting ``AgentEvent`` objects are delivered back
on the Qt main thread via a queued signal connection.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable

from PySide6.QtCore import QObject, Signal

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType

Emit = Callable[[AgentEvent], Awaitable[None]]
Handler = Callable[[AppRequest, Emit], Awaitable[None]]


class EventBus(QObject):
    """Marshals AppRequest -> background handler -> AgentEvent (back on UI thread)."""

    # Emitted on the Qt main thread (queued connection) for every AgentEvent.
    event = Signal(object)

    def __init__(self, handler: Handler, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handler = handler
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="pillow-bus", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, request: AppRequest) -> None:
        """Schedule a request for processing on the background loop."""
        asyncio.run_coroutine_threadsafe(self._process(request), self._loop)

    async def _process(self, request: AppRequest) -> None:
        async def emit(ev: AgentEvent) -> None:
            # Signal emission from a worker thread is delivered to main-thread
            # slots via Qt's queued connection mechanism.
            self.event.emit(ev)

        try:
            await self._handler(request, emit)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self.event.emit(AgentEvent(request_id=request.id, type=EventType.ERROR, text=str(exc)))

    def shutdown(self) -> None:
        """Stop the background loop; safe to call from the Qt main thread."""
        self._loop.call_soon_threadsafe(self._loop.stop)
