"""Pause and resume a running request between steps.

The agent loop calls :func:`checkpoint` before each model call; while the request
is paused it waits there (a model call or a tool already running finishes first).
Stopping stays :meth:`EventBus.cancel`. Everything here runs on the bus loop.
"""

from __future__ import annotations

import asyncio

_gates: dict[str, asyncio.Event] = {}  # request_id -> set while it may run
_inbox: dict[str, list[str]] = {}  # request_id -> words the user added while it ran
DEFAULT_MAX_TASKS = 3


def max_tasks() -> int:
    """Tasks that may run at once (settings ``max_tasks``, set by talking to Pillow)."""
    from pillow_assistant.core.settings import load_settings

    try:
        return max(1, min(20, int(load_settings().get("max_tasks") or DEFAULT_MAX_TASKS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TASKS


def tell(request_id: str, text: str) -> None:
    """Queue words for a running task; it reads them before its next step."""
    if request_id and text:
        _inbox.setdefault(request_id, []).append(text)


def take_messages(request_id: str) -> list[str]:
    return _inbox.pop(request_id, [])


def _gate(request_id: str) -> asyncio.Event:
    gate = _gates.get(request_id)
    if gate is None:
        gate = _gates[request_id] = asyncio.Event()
        gate.set()
    return gate


def pause(request_id: str) -> None:
    _gate(request_id).clear()


def resume(request_id: str) -> None:
    _gate(request_id).set()


def paused(request_id: str) -> bool:
    gate = _gates.get(request_id)
    return gate is not None and not gate.is_set()


async def checkpoint(request_id: str) -> None:
    """Wait here while the request is paused."""
    if request_id:
        await _gate(request_id).wait()


def forget(request_id: str) -> None:
    _gates.pop(request_id, None)
    _inbox.pop(request_id, None)
