"""Tests for the ask-user round trip (broker + tool)."""

import asyncio

from pillow_assistant.contracts import EventType
from pillow_assistant.core.ask import AskBroker
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.ask_tool import AskUserTool


def _ask_via(broker, request_id="r1"):
    events = []

    async def emit(ev):
        events.append(ev)

    async def ask(spec):
        return await broker.ask(emit, request_id, spec)

    return ask, events


def test_choice_resolves():
    broker = AskBroker()
    ask, events = _ask_via(broker)
    ctx = ToolContext(workspace=".", ask=ask)

    async def run():
        task = asyncio.ensure_future(AskUserTool()({"question": "选哪个？",
                                                    "options": ["A", "B"]}, ctx))
        await asyncio.sleep(0)  # let the ASK event emit
        ev = events[-1]
        assert ev.type == EventType.ASK
        assert ev.meta["allow_text"] is False  # options -> no free text by default
        broker.resolve(ev.meta["ask_id"], {"answer": "B", "cancelled": False})
        return await task

    r = asyncio.run(run())
    assert r.ok and "B" in r.text


def test_free_text_default():
    broker = AskBroker()
    ask, events = _ask_via(broker, "r2")
    ctx = ToolContext(workspace=".", ask=ask)

    async def run():
        task = asyncio.ensure_future(AskUserTool()({"question": "名字?"}, ctx))
        await asyncio.sleep(0)
        ev = events[-1]
        assert ev.meta["allow_text"] is True  # no options -> free text on
        broker.resolve(ev.meta["ask_id"], {"answer": "lobster", "cancelled": False})
        return await task

    r = asyncio.run(run())
    assert r.ok and "lobster" in r.text


def test_cancel():
    broker = AskBroker()
    ask, events = _ask_via(broker, "r3")
    ctx = ToolContext(workspace=".", ask=ask)

    async def run():
        task = asyncio.ensure_future(AskUserTool()({"question": "?",
                                                    "options": ["x"]}, ctx))
        await asyncio.sleep(0)
        broker.resolve(events[-1].meta["ask_id"], {"answer": "", "cancelled": True})
        return await task

    r = asyncio.run(run())
    assert not r.ok


def test_no_ask_capability():
    ctx = ToolContext(workspace=".")  # no ask
    r = asyncio.run(AskUserTool()({"question": "hi"}, ctx))
    assert not r.ok


def test_empty_question():
    broker = AskBroker()
    ask, _ = _ask_via(broker)
    ctx = ToolContext(workspace=".", ask=ask)
    r = asyncio.run(AskUserTool()({"question": "   "}, ctx))
    assert not r.ok


def test_resolve_unknown_is_noop():
    AskBroker().resolve("nope", {"answer": "x"})  # must not raise
