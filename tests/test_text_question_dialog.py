"""A question the model asks in plain text at the end of its answer becomes a dialog."""

import asyncio

from pillow_assistant.core import llm
from pillow_assistant.core.agent.loop import ToolLoopAgent, trailing_question
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.registry import ToolRegistry


def test_only_a_closing_question_counts():
    assert trailing_question("程序已生成。\n\n**需要我打开预览图让您检查刀路吗？**") == "需要我打开预览图让您检查刀路吗？"
    assert trailing_question("你要的是这个吗？\n程序已生成。") == ""


def _run(monkeypatch, tmp_path, replies, answer):
    seen, asked = [], []

    async def fake(**kwargs):
        seen.append([dict(m) for m in kwargs["messages"]])
        return llm.ToolTurn(content=replies[min(len(seen), len(replies)) - 1])

    async def ask(spec):
        asked.append(spec)
        return answer

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    ctx = ToolContext(workspace=str(tmp_path))
    ctx.ask = ask

    async def emit(event):
        pass

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, ToolRegistry(), ctx)
    final = asyncio.run(agent.run(prompt="出程序", emit=emit, request_id="r"))
    return final, seen, asked


def test_the_answer_goes_back_to_the_model(monkeypatch, tmp_path):
    final, seen, asked = _run(monkeypatch, tmp_path, ["程序好了。需要我打开预览图吗？", "已打开预览图。"],
                              {"answer": "好的", "cancelled": False})
    assert asked[0]["question"] == "程序好了。需要我打开预览图吗？" and asked[0]["options"]
    assert final == "已打开预览图。" and "好的" in seen[1][-1]["content"]


def test_a_dismissed_dialog_leaves_the_answer_as_it_was(monkeypatch, tmp_path):
    final, seen, asked = _run(monkeypatch, tmp_path, ["要不要再加一个倒角？"], {"answer": "", "cancelled": True})
    assert final == "要不要再加一个倒角？" and len(seen) == 1 and len(asked) == 1
