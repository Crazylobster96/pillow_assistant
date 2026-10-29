"""Windows Smart App Control blocks unsigned DLLs; Pillow says so instead of a cryptic error."""

import asyncio

from pillow_assistant.core import llm, sac
from pillow_assistant.core.agent.loop import ToolLoopAgent
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.registry import ToolRegistry

LOAD_ERROR = "ImportError: DLL load failed while importing _OCP: 应用程序控制策略已阻止此文件。"


def test_a_blocked_load_counts_only_while_the_control_is_on(monkeypatch):
    monkeypatch.setattr(sac, "state", lambda: sac.ON)
    assert sac.blocked(LOAD_ERROR) and not sac.blocked("ValueError: bad fillet")
    monkeypatch.setattr(sac, "state", lambda: sac.OFF)
    assert not sac.blocked(LOAD_ERROR)


def test_the_user_is_told_once_per_run(monkeypatch, tmp_path):
    monkeypatch.setattr(sac, "state", lambda: sac.ON)

    class Cad:
        name, description, permission = "text_to_cad", "cad", Permission.READONLY
        parameters = {"type": "object", "properties": {}}

        async def __call__(self, args, ctx):
            return ToolResult(ok=False, text=LOAD_ERROR)

    seen = []

    async def fake(**kwargs):
        seen.append([dict(m) for m in kwargs["messages"]])
        if len(seen) <= 2:
            return llm.ToolTurn(tool_calls=[llm.ToolCall(id=f"c{len(seen)}", name="text_to_cad", arguments="{}")])
        return llm.ToolTurn(content="做不了")

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    registry = ToolRegistry()
    registry.register(Cad())
    out = []

    async def emit(event):
        out.append(event.text or "")

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, registry, ToolContext(workspace=str(tmp_path)))
    asyncio.run(agent.run(prompt="做一块板", emit=emit, request_id="r"))
    assert sum("⚠ 这台电脑开启了" in text or "⚠ Windows Smart App Control is on" in text for text in out) == 1
    tool_texts = [m["content"] for m in seen[1] if m["role"] == "tool"]
    assert "不要反复重试" in tool_texts[0] or "do not retry" in tool_texts[0]
