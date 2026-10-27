"""Images a tool returns (file_read on a render) reach vision models and are dropped again."""

from __future__ import annotations

import asyncio
import json

from pillow_assistant.core import llm
from pillow_assistant.core.agent import loop as loop_mod
from pillow_assistant.core.agent.loop import ToolLoopAgent
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.file_tool import FileReadTool
from pillow_assistant.core.tools.registry import ToolRegistry

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def _run(monkeypatch, tmp_path, model, steps=1, refuse_images=False):
    (tmp_path / "view.png").write_bytes(PNG)
    seen = []

    async def fake(**kwargs):
        messages = kwargs["messages"]
        has_image = any(isinstance(m.get("content"), list) for m in messages)
        if refuse_images and has_image:
            raise RuntimeError("image input not supported")
        seen.append([dict(m) for m in messages])
        if len(seen) <= steps:
            return llm.ToolTurn(tool_calls=[llm.ToolCall(id=f"c{len(seen)}", name="file_read",
                                                         arguments=json.dumps({"path": "view.png"}))])
        return llm.ToolTurn(content="done")

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    registry = ToolRegistry()
    registry.register(FileReadTool())

    async def emit(event):
        pass

    agent = ToolLoopAgent({"provider": "openai", "model": model}, None, registry,
                          ToolContext(workspace=str(tmp_path)), max_steps=10)
    asyncio.run(agent.run(prompt="看图", emit=emit, request_id="r"))
    return seen, agent


def _images(messages):
    return [part for m in messages if isinstance(m.get("content"), list) for part in m["content"]
            if part.get("type") == "image_url"]


def test_vision_model_sees_the_render_after_the_tool_results(monkeypatch, tmp_path):
    seen, agent = _run(monkeypatch, tmp_path, "qwen-vl-max")
    last = seen[1]
    assert [m["role"] for m in last[-3:]] == ["assistant", "tool", "user"]
    assert _images(last)[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert not _images(agent.final_messages)  # not kept in the saved transcript


def test_text_model_is_told_it_cannot_see(monkeypatch, tmp_path):
    seen, _ = _run(monkeypatch, tmp_path, "deepseek-chat")
    assert not _images(seen[1])
    assert "不支持看图" in seen[1][-1]["content"] or "cannot take images" in seen[1][-1]["content"]


def test_old_images_are_dropped(monkeypatch, tmp_path):
    seen, _ = _run(monkeypatch, tmp_path, "qwen-vl-max", steps=loop_mod.IMAGE_KEEP_STEPS + 2)
    assert len(_images(seen[-1])) == loop_mod.IMAGE_KEEP_STEPS  # the two oldest became a note
    assert any("file_read" in str(m["content"]) for m in seen[-1] if m["role"] == "user")


def test_an_endpoint_that_refuses_images_is_retried_without_them(monkeypatch, tmp_path):
    seen, agent = _run(monkeypatch, tmp_path, "qwen-vl-max", refuse_images=True)
    assert len(seen) == 2 and not _images(seen[1]) and agent._vision is False


def test_vision_flag_in_extra_wins_and_is_not_sent():
    assert llm.supports_vision("openai", "my-model", {"vision": True})
    assert not llm.supports_vision("openai", "qwen-vl-max", {"vision": False})
    assert "vision" not in llm._split_context_config({"vision": True, "temperature": 0})[0]
