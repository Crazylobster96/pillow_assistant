"""The user's current request survives every compaction pass.

With a 16K default window and dozens of tool schemas, the final
fitting pass halved the last user message and cut the request out of it:
the model answered "tell me what you want to do" to a full NEMA17 plate spec.
"""

from __future__ import annotations

from pillow_assistant.core import context_budget as cb


def test_shorten_text_keeps_the_request_block_whole():
    request = "设计一块 NEMA17 步进电机安装板：80×60×6 mm，四角 R6。导出 STEP 和 STL。"
    text = cb.join_context_and_prompt("背景资料。" * 400, request)
    short = cb._shorten_text(text, 300)
    assert len(short) < len(text)
    assert f"{cb.REQUEST_OPEN}\n{request}\n{cb.REQUEST_CLOSE}" in short


def test_tight_budget_compacts_around_the_request():
    request = "合肥的顺序调为瑶海、肥西、肥东、长丰、巢湖、庐江"
    messages = [
        {"role": "system", "content": "系统提示。" * 300},
        {"role": "user", "content": cb.join_context_and_prompt("历史与项目状态。" * 2000, request)},
    ]
    tools = [{"type": "function", "function": {"name": f"tool_{i}", "description": "说明" * 400,
                                               "parameters": {"type": "object", "properties": {}}}}
             for i in range(40)]
    result = cb.ContextBudgetManager().prepare(
        provider="openai", api_base="http://x", model="unknown-model", messages=messages, tools=tools,
        model_info=None, context_config={"context_window": 16_384}, completion_extra=None,
    )
    assert result.compacted
    assert request in result.messages[-1]["content"]


def test_skill_instructions_stay_out_of_the_dialog():
    import asyncio
    from types import SimpleNamespace

    from pillow_assistant.contracts import EventType
    from pillow_assistant.core.agent.loop import ToolLoopAgent
    from pillow_assistant.core.tools.base import ToolResult

    class Registry:
        async def dispatch(self, name, args, ctx):
            return ToolResult(ok=True, text="【技能 excel】（按段重排、查找替换……）" * 50)

    agent = ToolLoopAgent(cfg={}, api_key=None, registry=Registry(), ctx=SimpleNamespace(audit=None))
    agent._artifacts = []
    shown = []

    async def emit(ev):
        if ev.type in (EventType.TOKEN, EventType.TOOL_RESULT):
            shown.append(ev.text)

    call = SimpleNamespace(id="1", name="apply_skill", arguments='{"name": "excel"}')
    full = asyncio.run(agent._run_tool(call, emit, "r1"))
    assert "按段重排、查找替换" in full  # the model still gets the instructions
    assert not any("按段重排、查找替换" in s for s in shown)
    assert any("excel" in s for s in shown)


def test_hosted_models_get_512k_local_servers_stay_small():
    assert cb.default_context_window(None) == 524_288
    assert cb.default_context_window("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1") == 524_288
    for local in ("http://localhost:11434", "http://127.0.0.1:1234/v1", "http://192.168.1.5:8000",
                  "http://172.20.0.2:8000", "http://box.local:8080"):
        assert cb.default_context_window(local) == cb.DEFAULT_CONTEXT_WINDOW, local


def test_unknown_hosted_model_is_budgeted_at_512k():
    import asyncio
    from types import SimpleNamespace

    from pillow_assistant.core import llm

    seen = {}

    class FakeCoordinator:
        async def manage(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(messages=kwargs["messages"])

    fake_litellm = SimpleNamespace(get_model_info=lambda name: (_ for _ in ()).throw(Exception("unknown")))
    orig = llm.semantic_context.get_semantic_coordinator
    llm.semantic_context.get_semantic_coordinator = lambda: FakeCoordinator()
    try:
        for base, window in (("https://dashscope.aliyuncs.com/v1", 524_288), ("http://localhost:11434", 32_768)):
            asyncio.run(llm._managed_request(
                fake_litellm, provider="OpenAI", model="qwen3.8-max", api_key="k", api_base=base,
                messages=[{"role": "user", "content": "hi"}], tools=None, completion_extra={}, context_config={}))
            assert seen["context_config"]["context_window"] == window
        asyncio.run(llm._managed_request(
            fake_litellm, provider="OpenAI", model="qwen3.8-max", api_key="k", api_base="https://x/v1",
            messages=[{"role": "user", "content": "hi"}], tools=None, completion_extra={},
            context_config={"context_window": 131_072}))
        assert seen["context_config"]["context_window"] == 131_072  # an explicit setting wins
    finally:
        llm.semantic_context.get_semantic_coordinator = orig
