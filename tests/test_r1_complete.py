"""R1++ tests (non-GUI): 3-way triage, project sessions/history, orchestrator
chat-vs-project routing.

Run: ``python tests/test_r1_complete.py``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.contracts import AppRequest, EventType
from pillow_assistant.core import llm
from pillow_assistant.core.orchestrator import Orchestrator
from pillow_assistant.core.project_manager import ProjectManager
from pillow_assistant.core.session import Session
from pillow_assistant.core.triage import TriageResult, is_app_setting_request, parse_triage
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


def test_sessions_and_index():
    print("project sessions + history + index")
    with tempfile.TemporaryDirectory() as d:
        store = ProjectStore(d)
        p = store.create("销售分析")
        s1 = store.new_session_id()
        store.record_turn(p, s1, "把CSV画成折线图", "已生成 sales.png")
        store.record_turn(p, s1, "加个标题", "已更新")
        s2 = store.new_session_id()
        store.record_turn(p, s2, "另一个问题", "另一个回答")
        check("session1 history = 4", len(store.load_history(p, s1)) == 4)
        check("session2 history = 2", len(store.load_history(p, s2)) == 2)
        check("all-sessions history = 6", len(store.load_history(p, None, max_turns=99)) == 6)
        check("two sessions listed", len(store.list_sessions(p)) == 2)
        idx = store.index()
        check("index last_prompt", idx and idx[0]["last_prompt"] == "另一个问题")


def test_app_settings_never_attach_to_projects():
    assert is_app_setting_request("将对话窗口的透明度调整至 15%")
    assert is_app_setting_request("展示窗口再透明一点")
    assert is_app_setting_request("change window opacity to 40%")
    assert is_app_setting_request("设置默认对话模型")
    assert not is_app_setting_request("继续完善 Web 播放器的播放列表")

def test_parse_triage_three_way():
    print("parse_triage (chat / continue / new)")
    ids = {"p1", "p2"}
    check("continue valid", parse_triage('{"action":"continue","project_id":"p1","confidence":0.9}', ids).action == "continue")
    check("continue invalid id -> chat", parse_triage('{"action":"continue","project_id":"ghost"}', ids).action == "chat")
    check("new", parse_triage('{"action":"new","name":"任务"}', ids).action == "new")
    check("chat explicit", parse_triage('{"action":"chat","confidence":0.8}', ids).action == "chat")
    check("garbage -> chat", parse_triage("nope", ids).action == "chat")


def test_apply_sessions():
    print("project_manager.apply (binds project + session)")
    with tempfile.TemporaryDirectory() as d:
        store = ProjectStore(d)
        session = Session()
        pm = ProjectManager(store, session)
        new = pm.apply(TriageResult(action="new", name="全新", confidence=0.9), "做个新东西")
        check("new project + session", session.project_id == new.id and session.session_id is not None)
        first_sid = session.session_id
        # continuing the SAME project keeps the session
        same = pm.apply(TriageResult(action="continue", project_id=new.id, confidence=0.9), "继续")
        check("continue same project keeps session", same.id == new.id and session.session_id == first_sid)
        # continuing a DIFFERENT project starts a new session
        other = store.create("别的项目")
        pm.apply(TriageResult(action="continue", project_id=other.id, confidence=0.9), "切过去")
        check("switch project -> new session", session.project_id == other.id and session.session_id != first_sid)


def _patch_fakes(triage_action, captured):
    async def fake_triage(prompt, index, *, cfg, api_key, current_id=None):
        if triage_action == "continue" and index:
            return TriageResult(action="continue", project_id=index[0]["id"], confidence=0.95)
        return TriageResult(action=triage_action, name="新", confidence=0.95)

    async def fake_cwt(**kwargs):
        msgs = kwargs.get("messages", [])
        captured["history_len"] = sum(1 for m in msgs if m.get("role") in ("user", "assistant")) - 1
        return llm.ToolTurn(content="好的，已完成。", tool_calls=[])

    import pillow_assistant.core.orchestrator as orch
    orch.triage = fake_triage
    llm.complete_with_tools = fake_cwt


def test_orchestrator_continue():
    print("orchestrator: continue feeds session history")
    captured = {}
    import pillow_assistant.core.orchestrator as orch
    orig_tr, orig_cwt = orch.triage, llm.complete_with_tools
    _patch_fakes("continue", captured)
    try:
        with tempfile.TemporaryDirectory() as d:
            store = ProjectStore(d)
            existing = store.create("延续项目")
            session = Session()
            session.project_id = existing.id
            session.session_id = store.new_session_id()
            store.record_turn(existing, session.session_id, "第一轮问题", "第一轮回答")
            pm = ProjectManager(store, session)

            class FakeStorage:
                def get_model_config(self, ref):
                    return {"display_name": "m", "provider": "OpenAI", "model": "x", "base_url": None, "extra": None}

            events = []

            async def emit(ev):
                events.append(ev)

            async def run():
                await Orchestrator(FakeStorage(), None, pm)(AppRequest(prompt="第二轮问题", model_ref="m"), emit)

            asyncio.run(run())
            check("continued same project", session.project_id == existing.id)
            check("session history fed (2 prior)", captured.get("history_len") == 2)
            check("turn recorded to session", len(store.load_history(existing, session.session_id)) == 4)
            check("ends DONE", events[-1].type == EventType.DONE)
            check("project note shown", any(existing.name in e.text for e in events if e.type == EventType.TOKEN))
    finally:
        orch.triage, llm.complete_with_tools = orig_tr, orig_cwt


def test_orchestrator_chat():
    print("orchestrator: chat creates no project")
    captured = {}
    import pillow_assistant.core.orchestrator as orch
    orig_tr, orig_cwt = orch.triage, llm.complete_with_tools
    _patch_fakes("chat", captured)
    try:
        with tempfile.TemporaryDirectory() as d:
            store = ProjectStore(d)
            session = Session()
            pm = ProjectManager(store, session)

            class FakeStorage:
                def get_model_config(self, ref):
                    return {"display_name": "m", "provider": "OpenAI", "model": "x", "base_url": None, "extra": None}

            events = []

            async def emit(ev):
                events.append(ev)

            async def run():
                await Orchestrator(FakeStorage(), None, pm)(AppRequest(prompt="你好", model_ref="m"), emit)

            asyncio.run(run())
            check("no project created", len(store.list()) == 0)
            check("session not bound", session.project_id is None)
            check("chat note shown", any("💬 对话" in e.text for e in events if e.type == EventType.TOKEN))
            check("ends DONE", events[-1].type == EventType.DONE)
    finally:
        orch.triage, llm.complete_with_tools = orig_tr, orig_cwt


def test_low_confidence_switch_does_not_shadow_triage_result():
    """A below-threshold cross-project suggestion must fall back to chat."""
    import pillow_assistant.core.orchestrator as orch
    original_triage = orch.triage

    async def fake_triage(prompt, index, *, cfg, api_key, current_id=None):
        return TriageResult(
            action="continue", project_id=index[0]["id"], confidence=0.5
        )

    orch.triage = fake_triage
    try:
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(directory)
            store.create("target")
            session = Session()
            manager = ProjectManager(store, session)

            class FakeStorage:
                def get_model_config(self, ref):
                    return {
                        "display_name": "m", "provider": "OpenAI", "model": "x",
                        "base_url": None, "extra": None,
                    }

            called = []
            orchestrator = Orchestrator(FakeStorage(), None, manager)

            async def fake_run_chat(*args, **kwargs):
                called.append(True)

            orchestrator._run_chat = fake_run_chat

            async def emit(event):
                pass

            asyncio.run(orchestrator(AppRequest(prompt="more transparent", model_ref="m"), emit))
            assert called == [True]
            assert session.project_id is None
    finally:
        orch.triage = original_triage

if __name__ == "__main__":
    for t in (test_sessions_and_index, test_parse_triage_three_way, test_apply_sessions,
              test_orchestrator_continue, test_orchestrator_chat):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
