"""Event bus bridging the Qt UI thread and a background asyncio loop.

The UI calls :meth:`EventBus.submit` from the Qt main thread. The request is
processed on a dedicated asyncio loop running in its own thread, so model calls
never block the UI (NFR-1). Resulting ``AgentEvent`` objects are delivered back
on the Qt main thread via a queued signal connection.

:meth:`EventBus.pause` / :meth:`EventBus.resume` hold a request before its next
step (``core/run_control.py``). :meth:`EventBus.cancel` stops a running request: its task is cancelled at the
next await (a model call, a tool, a question to the user) and the UI gets a
DONE event with ``meta["cancelled"]``.
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
        self._tasks: dict[str, asyncio.Task] = {}  # touched only on the bus loop
        self._running = 0  # requests past the limit wait on _slots (bus loop only)
        self._slots: asyncio.Condition | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="pillow-bus", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, request: AppRequest) -> None:
        """Schedule a request for processing on the background loop."""
        asyncio.run_coroutine_threadsafe(self._process(request), self._loop)

    def cancel(self, request_id: str) -> None:
        """Stop a running request; safe to call from the Qt main thread."""
        self._loop.call_soon_threadsafe(self._cancel, request_id)

    def pause(self, request_id: str) -> None:
        """Hold a running request before its next step; safe from the Qt main thread."""
        from pillow_assistant.core import run_control

        self._loop.call_soon_threadsafe(run_control.pause, request_id)

    def resume(self, request_id: str) -> None:
        from pillow_assistant.core import run_control

        self._loop.call_soon_threadsafe(run_control.resume, request_id)

    def tell(self, request_id: str, text: str) -> None:
        """Add the user's words to a running request; it reads them before its next step."""
        from pillow_assistant.core import run_control

        self._loop.call_soon_threadsafe(run_control.tell, request_id, text)

    def _cancel(self, request_id: str) -> None:
        task = self._tasks.get(request_id)
        if task is not None and not task.done():
            task.cancel()

    async def _process(self, request: AppRequest) -> None:
        async def emit(ev: AgentEvent) -> None:
            # Signal emission from a worker thread is delivered to main-thread
            # slots via Qt's queued connection mechanism.
            self.event.emit(ev)

        from pillow_assistant.core import run_control
        from pillow_assistant.core.i18n import t

        task = asyncio.current_task()
        if task is not None:
            self._tasks[request.id] = task
        if self._slots is None:
            self._slots = asyncio.Condition()
        started = False
        try:
            async with self._slots:
                if self._running >= run_control.max_tasks():
                    self.event.emit(AgentEvent(request_id=request.id, type=EventType.TOOL_START, text="",
                                               meta={"status": t("task.queued", n=run_control.max_tasks()),
                                                     "queued": True}))
                await self._slots.wait_for(lambda: self._running < run_control.max_tasks())
                self._running += 1
                started = True
            await self._handler(request, emit)
        except asyncio.CancelledError:
            self.event.emit(AgentEvent(request_id=request.id, type=EventType.DONE,
                                       meta={"cancelled": True}))
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self.event.emit(AgentEvent(request_id=request.id, type=EventType.ERROR, text=str(exc)))
        finally:
            self._tasks.pop(request.id, None)
            run_control.forget(request.id)
            if started:
                async with self._slots:
                    self._running -= 1
                    self._slots.notify_all()

    def shutdown(self) -> None:
        """Stop the background loop; safe to call from the Qt main thread."""
        self._loop.call_soon_threadsafe(self._loop.stop)
