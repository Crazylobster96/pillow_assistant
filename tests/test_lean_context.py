"""Per-step timing, skill-only tools held back until apply_skill, old reference text evicted."""

from __future__ import annotations

import asyncio
import json

from pillow_assistant.core import llm
from pillow_assistant.core.agent.loop import ToolLoopAgent
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.registry import ToolRegistry


class _Tool:
    permission = Permission.READONLY
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name, text="ok", on_call=None):
        self.name, self.description, self.text, self.on_call = name, name, text, on_call

    async def __call__(self, args, ctx):
        if self.on_call:
            self.on_call(ctx)
        return ToolResult(ok=True, text=self.text)


class _Audit:
    def __init__(self):
        self.rows = []

    def _write(self, row):
        self.rows.append(row)

    def tool_call(self, *args, **kwargs):
        pass


def _run(monkeypatch, tmp_path, script, registry):
    """script: list of (tool_name, args) per step; a final answer follows."""
    seen = []

    async def fake(**kwargs):
        seen.append(([t["function"]["name"] for t in kwargs["tools"]],
                     [dict(m) for m in kwargs["messages"]]))
        step = len(seen)
        usage = {"prompt_tokens": 1000 * step, "completion_tokens": 10}
        if step <= len(script):
            name, args = script[step - 1]
            return llm.ToolTurn(tool_calls=[llm.ToolCall(id=f"c{step}", name=name,
                                                         arguments=json.dumps(args))], usage=usage)
        return llm.ToolTurn(content="done", usage=usage)

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    audit = _Audit()
    ctx = ToolContext(workspace=str(tmp_path), audit=audit)
    events = []

    async def emit(event):
        events.append(event)

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, registry, ctx, max_steps=20)
    asyncio.run(agent.run(prompt="做个零件", emit=emit, request_id="r"))
    return seen, audit, events, agent


def _registry():
    registry = ToolRegistry()
    unlock = lambda ctx: ctx.active_tools.update({"text_to_cad", "cad_reference"})  # noqa: E731
    for tool in (_Tool("file_read"), _Tool("apply_skill", on_call=unlock), _Tool("text_to_cad"),
                 _Tool("cad_reference", text="R" * 5000), _Tool("excel_edit")):
        registry.register(tool)
    registry.skill_only = {"text_to_cad", "cad_reference", "excel_edit"}
    return registry


def test_skill_tools_are_offered_only_after_apply_skill(monkeypatch, tmp_path):
    seen, *_ = _run(monkeypatch, tmp_path, [("apply_skill", {"name": "cad"}), ("text_to_cad", {})], _registry())
    assert seen[0][0] == ["file_read", "apply_skill"]
    assert seen[1][0] == ["file_read", "apply_skill", "text_to_cad", "cad_reference"]  # excel stays out


def test_every_model_call_is_timed_and_summed(monkeypatch, tmp_path):
    seen, audit, events, agent = _run(monkeypatch, tmp_path, [("file_read", {})], _registry())
    calls = [r for r in audit.rows if r["kind"] == "llm_call"]
    assert [c["step"] for c in calls] == [1, 2] and calls[1]["input_tokens"] == 2000
    perf = [r for r in audit.rows if r["kind"] == "run_perf"][0]
    assert perf["calls"] == 2 and perf["input_tokens"] == 3000 and perf["output_tokens"] == 20
    assert any("⏱" in (e.text or "") for e in events)
    assert agent.perf["model_s"] >= 0 and agent.perf["tool_s"] >= 0


def test_a_resumed_run_keeps_the_tools_it_already_unlocked(monkeypatch, tmp_path):
    seen = []

    async def fake(**kwargs):
        seen.append([t["function"]["name"] for t in kwargs["tools"]])
        return llm.ToolTurn(content="done")

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    resume = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
              {"role": "assistant", "content": "", "tool_calls": [
                  {"id": "c1", "type": "function", "function": {"name": "text_to_cad", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    ctx = ToolContext(workspace=str(tmp_path))

    async def emit(event):
        pass

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, _registry(), ctx, max_steps=3)
    asyncio.run(agent.run(prompt="继续", emit=emit, request_id="r", resume_messages=resume))
    assert "text_to_cad" in seen[0] and "excel_edit" not in seen[0]
