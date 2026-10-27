"""Tool calls a model wrote as text are run, or the model is asked to call again."""

from __future__ import annotations

import asyncio
import json

from pillow_assistant.core import llm
from pillow_assistant.core.agent import text_tool_calls
from pillow_assistant.core.agent.loop import ToolLoopAgent
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.registry import ToolRegistry

TOOLS = [
    {"type": "function", "function": {"name": "run_python", "parameters": {
        "type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "file_list", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}}}},
]

# What qwen3.7-plus once answered instead of making a call (shortened).
SEEN = ('好的，我来设计这个直角三角加强筋并导出 DXF。让我先创建脚本：\n\n\n[tool.run_python]\n<parameter name="code">\n'
        '"""2x2 layout."""\n\nprint("gusset")\n</parameter>\n</invoke>')


def test_the_text_from_the_gusset_run_becomes_a_call():
    before, calls = text_tool_calls.recover(SEEN, TOOLS)
    assert before.endswith("让我先创建脚本：")
    assert [c.name for c in calls] == ["run_python"]
    assert json.loads(calls[0].arguments) == {"code": '"""2x2 layout."""\n\nprint("gusset")'}


def test_other_spellings_and_typed_arguments():
    hermes = '<tool_call>{"name": "file_list", "arguments": {"path": "."}}</tool_call>'
    assert json.loads(text_tool_calls.recover(hermes, TOOLS)[1][0].arguments) == {"path": "."}
    xml = '<function=file_list>\n<parameter=path>\nsrc\n</parameter>\n<parameter=depth>\n2\n</parameter>\n</function>'
    assert json.loads(text_tool_calls.recover(xml, TOOLS)[1][0].arguments) == {"path": "src", "depth": 2}


def test_unreadable_calls_are_not_guessed():
    assert text_tool_calls.recover("[tool.delete_everything]\n<parameter name=\"x\">1</parameter>", TOOLS) is None
    assert text_tool_calls.recover('[tool.run_python]\n<parameter name="other">1</parameter>', TOOLS) is None  # no code
    assert text_tool_calls.recover("普通回答，没有调用", TOOLS) is None
    assert not text_tool_calls.looks_like_call("普通回答，没有调用")


class _Python:
    name = "run_python"
    description = "run"
    permission = Permission.READONLY
    parameters = TOOLS[0]["function"]["parameters"]

    def __init__(self):
        self.ran = []

    async def __call__(self, args, ctx):
        self.ran.append(args["code"])
        return ToolResult(ok=True, text="gusset")


def _run(monkeypatch, tmp_path, replies):
    seen = []

    async def fake(**kwargs):
        seen.append([dict(m) for m in kwargs["messages"]])
        return replies[min(len(seen), len(replies)) - 1]

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    registry, tool = ToolRegistry(), _Python()
    registry.register(tool)

    async def emit(event):
        pass

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, registry, ToolContext(workspace=str(tmp_path)),
                          max_steps=8)
    final = asyncio.run(agent.run(prompt="做个报表", emit=emit, request_id="r"))
    return final, tool, seen


def test_the_loop_runs_a_text_call_and_carries_on(monkeypatch, tmp_path):
    final, tool, seen = _run(monkeypatch, tmp_path, [llm.ToolTurn(content=SEEN), llm.ToolTurn(content="DXF 已导出")])
    assert tool.ran == ['"""2x2 layout."""\n\nprint("gusset")'] and final == "DXF 已导出"
    assert seen[1][-1]["role"] == "tool"


def test_the_loop_asks_again_when_it_cannot_read_the_call(monkeypatch, tmp_path):
    broken = llm.ToolTurn(content='[tool.run_python]\n<parameter name="cod">x</parameter>')
    final, tool, seen = _run(monkeypatch, tmp_path, [broken, broken, broken])
    reminder = seen[1][-1]["content"]
    assert len(seen) == 3 and ("函数调用" in reminder or "function call" in reminder)
    assert tool.ran == []  # after two reminders the text is the answer, as before
