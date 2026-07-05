from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.contracts import AppRequest, EventType
from pillow_assistant.core import llm
from pillow_assistant.core.conversation_memory import (
    ConversationContextBuilder,
    ConversationMemoryService,
    ConversationRouter,
    ConversationRoute,
    is_greeting,
    looks_one_off_qa,
)
from pillow_assistant.core.orchestrator import Orchestrator
from pillow_assistant.core.project_manager import ProjectManager
from pillow_assistant.core.session import Session
from pillow_assistant.core.triage import TriageResult
from storage import Storage
from storage.conversation import ConversationMemoryStore
from storage.projects import ProjectStore

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def cfg():
    return {"display_name": "m", "provider": "OpenAI", "model": "x", "base_url": None, "extra": None}


def test_store_schema_and_crud():
    print("conversation store")
    with tempfile.TemporaryDirectory() as d:
        store = ConversationMemoryStore(Path(d) / "assistant.db")
        store.ensure_schema()
        store.ensure_schema()
        topic = store.create_topic("Memory design", "Discuss topic memory", ["memory", "topic"])
        check("topic created", topic["id"].startswith("topic_") and topic["keywords"] == ["memory", "topic"])
        turn = store.append_turn(topic["id"], "用户说记忆系统", "助手回答", keywords=["memory"], importance=0.5)
        check("turn created", turn["topic_id"] == topic["id"])
        updated = store.get_topic(topic["id"])
        check("topic count updated", updated["message_count"] == 1 and updated["last_message_at"] is not None)
        recent = store.recent_turns(topic["id"], 3)
        check("recent turns", len(recent) == 1 and recent[0]["id"] == turn["id"])
        relevant = store.search_relevant_turns("记忆系统", 2)
        check("relevant search", relevant and relevant[0]["id"] == turn["id"])
        signal = store.add_user_memory_signal("preference", "分析源码时带文件路径", confidence=0.9, source_turn_id=turn["id"])
        check("signal stored", signal["content"] == "分析源码时带文件路径")


def test_router_rules():
    print("conversation router rules")
    check("greeting", is_greeting("你好"))
    check("one off qa", looks_one_off_qa("Python 怎么读 JSON？"))


def test_router_model_paths():
    print("conversation router model paths")
    with tempfile.TemporaryDirectory() as d:
        store = ConversationMemoryStore(Path(d) / "assistant.db")
        store.ensure_schema()
        topic = store.create_topic("Memory design", "Discuss memory routing", ["memory"])
        router = ConversationRouter(store)
        original = llm.complete

        async def fake_existing(**kwargs):
            return '{"kind":"existing_topic","topic_id":"%s","confidence":0.91,"reason":"same topic"}' % topic["id"]

        async def fake_new(**kwargs):
            return '{"kind":"new_topic","title":"Travel","summary":"Trip planning","keywords":["travel"],"confidence":0.88,"reason":"new"}'

        try:
            llm.complete = fake_existing
            route = asyncio.run(router.route("继续说记忆系统", cfg=cfg(), api_key=None))
            check("existing topic route", route.kind == "existing_topic" and route.topic_id == topic["id"])
            llm.complete = fake_new
            route = asyncio.run(router.route("周末旅行计划", cfg=cfg(), api_key=None))
            check("new topic route", route.kind == "new_topic" and route.title == "Travel")
        finally:
            llm.complete = original


def test_context_builder():
    print("conversation context builder")
    with tempfile.TemporaryDirectory() as d:
        store = ConversationMemoryStore(Path(d) / "assistant.db")
        store.ensure_schema()
        topic = store.create_topic("Memory design", "Discuss memory routing", ["memory"])
        other = store.create_topic("Source code", "Discuss orchestrator", ["orchestrator"])
        store.append_turn(topic["id"], "第一轮记忆", "回答一", user_summary="第一轮记忆")
        store.append_turn(other["id"], "Orchestrator 接入点", "回答二", user_summary="Orchestrator 接入点", importance=1.0)
        store.add_user_memory_signal("preference", "分析源码时带文件路径", confidence=0.9, status="active")
        ctx = ConversationContextBuilder(store).build(
            ConversationRoute(kind="existing_topic", topic_id=topic["id"]),
            "Orchestrator 里怎么接记忆？",
        )
        check("context has topic", ctx.topic["id"] == topic["id"])
        check("context has recent", len(ctx.recent_turns) == 1)
        check("context has relevant", ctx.relevant_turns and ctx.relevant_turns[0]["topic_id"] == other["id"])
        check("rendered has time/source", "Source topic" in ctx.rendered_context and "Recent turns" in ctx.rendered_context)


def test_writeback():
    print("conversation writeback")
    with tempfile.TemporaryDirectory() as d:
        store = ConversationMemoryStore(Path(d) / "assistant.db")
        store.ensure_schema()
        service = ConversationMemoryService(store)
        ctx = service.builder.build(
            ConversationRoute(kind="new_topic", title="Memory design", summary="Discuss memory", keywords=["memory"]),
            "以后分析源码请带文件路径",
        )
        original = llm.complete

        async def fake_complete(**kwargs):
            sys_msg = kwargs["messages"][0]["content"]
            if "Extract reusable" in sys_msg:
                return '[{"type":"preference","content":"分析源码时带文件路径","confidence":0.9,"status":"active","needs_confirmation":false}]'
            return '{"user_summary":"用户要求源码分析带路径","assistant_summary":"助手确认","topic_summary":"讨论源码分析偏好","keywords":["源码","路径"],"topic_keywords":["源码","路径"],"importance":0.8}'

        try:
            llm.complete = fake_complete
            asyncio.run(service.record_chat_result(ctx, "以后分析源码请带文件路径", "好的，以后会带路径。", cfg=cfg(), api_key=None))
            turns = store.recent_turns(ctx.route.topic_id, 5)
            signals = store.list_user_memory_signals(status="active")
            check("turn written", len(turns) == 1 and turns[0]["user_summary"] == "用户要求源码分析带路径")
            check("signal written", signals and signals[0]["content"] == "分析源码时带文件路径")
        finally:
            llm.complete = original


def test_orchestrator_chat_integration():
    print("orchestrator chat integration")
    with tempfile.TemporaryDirectory() as d:
        storage = Storage(Path(d) / "assistant.db")
        storage.ensure_schema()
        storage.replace_model_configs([{"provider": "OpenAI", "model_type": "llm", "display_name": "m", "model": "x"}])
        session = Session()
        pm = ProjectManager(ProjectStore(Path(d) / "projects"), session)
        events = []
        captured = {}

        import pillow_assistant.core.orchestrator as orch
        original_triage = orch.triage
        original_complete = llm.complete
        original_cwt = llm.complete_with_tools

        async def fake_triage(prompt, index, *, cfg, api_key, current_id=None):
            return TriageResult(action="chat", confidence=1.0)

        async def fake_complete(**kwargs):
            return '{"user_summary":"讨论记忆","assistant_summary":"回答记忆","topic_summary":"讨论顶层记忆","keywords":["记忆"],"topic_keywords":["记忆"],"importance":0.7}'

        async def fake_cwt(**kwargs):
            messages = kwargs.get("messages", [])
            captured["context"] = messages[-1].get("content")
            return llm.ToolTurn(content="这是回答", tool_calls=[])

        async def emit(ev):
            events.append(ev)

        try:
            orch.triage = fake_triage
            llm.complete = fake_complete
            llm.complete_with_tools = fake_cwt
            asyncio.run(Orchestrator(storage, None, pm)(AppRequest(prompt="我们讨论顶层记忆系统", model_ref="m"), emit))
            store = ConversationMemoryStore(storage.db_path)
            topics = store.list_recent_topics()
            check("topic created through orchestrator", len(topics) == 1)
            check("turn recorded through orchestrator", len(store.recent_turns(topics[0]["id"], 5)) == 1)
            check("ends done", events[-1].type == EventType.DONE)
        finally:
            orch.triage = original_triage
            llm.complete = original_complete
            llm.complete_with_tools = original_cwt


if __name__ == "__main__":
    for test in (
        test_store_schema_and_crud,
        test_router_rules,
        test_router_model_paths,
        test_context_builder,
        test_writeback,
        test_orchestrator_chat_integration,
    ):
        test()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
