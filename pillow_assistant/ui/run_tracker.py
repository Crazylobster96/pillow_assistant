"""Keep the transcript of running requests so a closed dialog can be reopened.

The dialogs (quick input bar, file panels) only render events while they are
open, and they are short-lived: Esc, a click elsewhere or the close button tears
them down while the Agent keeps working on the bus loop. The tracker listens to
the bus for the whole app lifetime and records, per request, the prompt, the
streamed answer and the latest event, so a newly opened dialog can show where
the run is and keep following it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject

from pillow_assistant.contracts import AgentEvent, EventType

KEEP_FINISHED = 5  # finished runs kept (newest first) besides the running ones


def is_dialog_text(event: AgentEvent) -> bool:
    """Whether a TOKEN belongs in the conversation (the answer, a notice for the user)
    rather than in the task panel (steps, tool output, checks, timings)."""
    meta = event.meta or {}
    return bool(meta.get("answer") or meta.get("notice"))


@dataclass
class Run:
    request_id: str
    prompt: str
    text: str = ""  # what the dialog shows: the answer and notices
    log: str = ""  # the task's process: steps, tool output, checks
    is_task: bool = False  # a tool ran: it has a task page
    started: float = 0.0
    last_event: Optional[AgentEvent] = None
    running: bool = True
    cancelled: bool = False
    seen: bool = False  # shown to the user in an open dialog after it finished
    order: int = 0


@dataclass
class _State:
    runs: dict[str, Run] = field(default_factory=dict)
    counter: int = 0


class RunTracker(QObject):
    def __init__(self, bus=None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state = _State()
        if bus is not None:
            bus.event.connect(self.on_event)

    def start(self, request_id: str, prompt: str) -> None:
        self._state.counter += 1
        self._state.runs[request_id] = Run(request_id, prompt, order=self._state.counter, started=time.time())
        self._prune()

    def get(self, request_id: Optional[str]) -> Optional[Run]:
        return self._state.runs.get(request_id) if request_id else None

    def latest_running(self) -> Optional[Run]:
        running = [r for r in self._state.runs.values() if r.running]
        return max(running, key=lambda r: r.order) if running else None

    def on_event(self, event: AgentEvent) -> None:
        run = self._state.runs.get(getattr(event, "request_id", None))
        if run is None:
            return
        if event.type == EventType.TOKEN:
            if is_dialog_text(event):
                run.text += event.text or ""
            else:
                run.log += event.text or ""
        elif event.type == EventType.TOOL_START and not (event.meta or {}).get("status"):
            run.is_task = True
        elif event.type in (EventType.DONE, EventType.ERROR):
            run.running = False
            run.cancelled = bool((event.meta or {}).get("cancelled"))
        if event.type != EventType.TOKEN:
            run.last_event = event

    def _prune(self) -> None:
        finished = sorted((r for r in self._state.runs.values() if not r.running),
                          key=lambda r: r.order, reverse=True)
        for run in finished[KEEP_FINISHED:]:
            self._state.runs.pop(run.request_id, None)
