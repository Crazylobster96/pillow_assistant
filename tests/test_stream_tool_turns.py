"""Tool turns arrive as a stream: tool-call pieces are joined, progress is reported."""

import asyncio
from types import SimpleNamespace as NS

from pillow_assistant.core import llm


def _chunks():
    yield NS(choices=[NS(delta=NS(content=None, reasoning_content="想" * 500, tool_calls=None))], usage=None)
    yield NS(choices=[NS(delta=NS(content="先写脚本。", tool_calls=None))], usage=None)
    yield NS(choices=[NS(delta=NS(content=None, tool_calls=[
        NS(index=0, id="call_1", function=NS(name="file_write", arguments='{"path": "a.mjs", '))]))], usage=None)
    for _ in range(3):
        yield NS(choices=[NS(delta=NS(content=None, tool_calls=[
            NS(index=0, id=None, function=NS(name=None, arguments='"x' + "y" * 300 + '"'))]))], usage=None)
    yield NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=20, total_tokens=30))


class _Stream:
    def __init__(self):
        self._it = _chunks()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def test_stream_is_assembled_into_content_calls_and_usage():
    seen = {}

    async def acompletion(**kwargs):
        seen.update(kwargs)
        return _Stream()

    progress = []

    async def on_progress(chars, thinking=0):
        progress.append((chars, thinking))

    content, calls, usage, stats = asyncio.run(llm._streamed_tool_turn(NS(acompletion=acompletion), {"model": "m"}, on_progress))
    assert stats["chunks"] == 7 and stats["first_chunk_ms"] is not None
    assert seen["stream"] is True and seen["timeout"] == llm.STREAM_IDLE_S
    assert content == "先写脚本。"
    assert [(c.id, c.name) for c in calls] == [("call_1", "file_write")]
    assert calls[0].arguments.startswith('{"path": "a.mjs", "x')
    assert usage.get("prompt_tokens") == 10 and progress[0] == (0, 500)  # thinking is reported too


class _Trickle:
    """Keep-alive chunks that carry nothing, forever."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.01)
        return NS(choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=None))], usage=None)


def test_empty_chunks_do_not_keep_a_stalled_call_alive(monkeypatch):
    monkeypatch.setattr(llm, "STREAM_IDLE_S", 0.2)

    async def acompletion(**kwargs):
        return _Trickle()

    import pytest
    with pytest.raises(llm.ModelStalled):
        asyncio.run(llm._streamed_tool_turn(NS(acompletion=acompletion), {"model": "m"}))


def test_a_call_that_never_answers_is_given_up(monkeypatch):
    monkeypatch.setattr(llm, "STREAM_IDLE_S", 0.2)

    async def acompletion(**kwargs):
        await asyncio.sleep(5)

    import pytest
    with pytest.raises(llm.ModelStalled):
        asyncio.run(llm._streamed_tool_turn(NS(acompletion=acompletion), {"model": "m"}))


def test_a_stalled_turn_is_asked_once_more(monkeypatch, tmp_path):
    from pillow_assistant.core.agent.loop import ToolLoopAgent
    from pillow_assistant.core.tools.base import ToolContext
    from pillow_assistant.core.tools.registry import ToolRegistry

    calls = []

    async def fake(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise llm.ModelStalled("nothing new")
        return llm.ToolTurn(content="好了")

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    out = []

    async def emit(event):
        out.append(event.text or "")

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, ToolRegistry(), ToolContext(workspace=str(tmp_path)))
    assert asyncio.run(agent.run(prompt="hi", emit=emit, request_id="r")) == "好了"
    assert len(calls) == 2 and any("⏳" in text for text in out)
