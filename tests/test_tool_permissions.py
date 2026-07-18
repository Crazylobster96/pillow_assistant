from __future__ import annotations

import asyncio

from pillow_assistant.contracts import EventType
from pillow_assistant.core.ask import AskBroker
from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.registry import ToolRegistry


class DummyTool:
    name = "dummy"
    description = "dummy"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, permission=Permission.READONLY):
        self.permission = permission
        self.called = False

    async def __call__(self, args, ctx):
        self.called = True
        return ToolResult(ok=True, text="executed")


def _dispatch(tool, *, ask=None, args=None):
    registry = ToolRegistry()
    registry.register(tool)
    context = ToolContext(workspace=".", ask=ask)
    return asyncio.run(registry.dispatch(tool.name, args or {}, context))


def test_readonly_and_workspace_tools_do_not_prompt():
    async def unexpected_ask(_spec):
        raise AssertionError("safe tools must not prompt")

    for permission in (Permission.READONLY, Permission.WRITE_WS):
        tool = DummyTool(permission)
        result = _dispatch(tool, ask=unexpected_ask)
        assert result.ok and tool.called


def test_elevated_tools_without_confirmation_ui_are_denied():
    for permission in (Permission.NETWORK, Permission.SYSTEM):
        tool = DummyTool(permission)
        result = _dispatch(tool)
        assert not result.ok
        assert not tool.called


def test_allow_once_executes_elevated_tool():
    captured = {}

    async def allow(spec):
        captured.update(spec)
        return {"answer": t("tool.permission.allow_once"), "cancelled": False}

    tool = DummyTool(Permission.SYSTEM)
    result = _dispatch(tool, ask=allow, args={"action": "change-setting"})

    assert result.ok and tool.called
    assert captured["permission"] == Permission.SYSTEM.value
    assert captured["allow_text"] is False


def test_denial_and_timeout_do_not_execute_tool():
    async def deny(_spec):
        return {"answer": t("tool.permission.deny"), "cancelled": False}

    async def timeout(_spec):
        return {"answer": "", "cancelled": True, "timeout": True}

    for ask in (deny, timeout):
        tool = DummyTool(Permission.NETWORK)
        result = _dispatch(tool, ask=ask)
        assert not result.ok
        assert not tool.called


def test_permission_prompt_redacts_secrets():
    captured = {}

    async def deny(spec):
        captured.update(spec)
        return {"answer": t("tool.permission.deny"), "cancelled": False}

    tool = DummyTool(Permission.SYSTEM)
    tool.name = "configure_model"
    _dispatch(tool, ask=deny, args={"display_name": "main", "api_key": "super-secret"})

    assert "super-secret" not in captured["question"]
    assert "[redacted]" in captured["question"]


def test_missing_permission_defaults_to_system_confirmation():
    tool = DummyTool()
    del tool.permission
    result = _dispatch(tool)

    assert not result.ok
    assert not tool.called


def test_permission_round_trip_through_ask_broker():
    broker = AskBroker()
    events = []
    tool = DummyTool(Permission.NETWORK)
    registry = ToolRegistry()
    registry.register(tool)

    async def emit(event):
        events.append(event)

    async def ask(spec):
        return await broker.ask(emit, "permission-request", spec)

    async def run():
        context = ToolContext(workspace=".", ask=ask)
        task = asyncio.create_task(registry.dispatch(tool.name, {"url": "https://example.com"}, context))
        await asyncio.sleep(0)
        event = events[-1]
        assert event.type == EventType.ASK
        assert event.meta["permission"] == Permission.NETWORK.value
        broker.resolve(event.meta["ask_id"], {
            "answer": t("tool.permission.allow_once"),
            "cancelled": False,
        })
        return await task

    result = asyncio.run(run())
    assert result.ok and tool.called
