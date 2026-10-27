"""References that reach the Agent, per-conversation tool permissions, stopping and
following runs (the four fixes for dropped files, permission prompts, reopening a
closed dialog and interrupting a run)."""

from __future__ import annotations

import asyncio
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType  # noqa: E402
from pillow_assistant.core.bus import EventBus  # noqa: E402
from pillow_assistant.core.i18n import t  # noqa: E402
from pillow_assistant.core.references import paths_in_text  # noqa: E402
from pillow_assistant.core.session import Session  # noqa: E402
from pillow_assistant.core.tools.base import Permission, ToolContext  # noqa: E402
from pillow_assistant.core.tools.registry import ToolRegistry  # noqa: E402
from pillow_assistant.ui.run_tracker import RunTracker  # noqa: E402
from tests.test_tool_permissions import DummyTool  # noqa: E402


def test_files_on_screen_go_with_the_question_without_becoming_chips(tmp_path):
    session = Session()
    a, b = str(tmp_path / "a.xlsx"), str(tmp_path / "b.pdf")
    session.add_reference(a)
    session.set_displayed([a, b])
    assert session.references == [a]
    assert session.request_references() == [a, b]
    session.remove_displayed(b)
    assert session.request_references() == [a]
    session.set_displayed([b])
    session.clear_displayed()
    assert session.request_references() == [a]


def test_paths_typed_in_a_message_are_picked_up(tmp_path):
    folder = tmp_path / "xby"
    folder.mkdir()
    (folder / "表.xlsx").write_text("x", "utf-8")
    text = f"文件在 {folder}，另外 {folder / '表.xlsx'}。还有 {tmp_path / 'missing.txt'} 和 a/b"
    assert paths_in_text(text) == [str(folder), str(folder / "表.xlsx")]
    assert paths_in_text("http://example.com/x and ./rel/path") == []


def _dispatch(tool, session, answers):
    asked = []

    async def ask(spec):
        asked.append(spec)
        return {"answer": answers.pop(0), "cancelled": False}

    registry = ToolRegistry()
    registry.register(tool)
    ctx = ToolContext(workspace=".", ask=ask, session=session)
    return asyncio.run(registry.dispatch(tool.name, {}, ctx)), asked


def test_allow_for_this_conversation_stops_asking_until_the_conversation_changes():
    session = Session()
    session.project_id, session.session_id = "p1", "s1"
    tool = DummyTool(Permission.SYSTEM)
    result, asked = _dispatch(tool, session, [t("tool.permission.allow_conversation")])
    assert result.ok and len(asked) == 1
    assert t("tool.permission.allow_conversation") in asked[0]["options"]
    result, asked = _dispatch(tool, session, [])
    assert result.ok and asked == []  # remembered: no second prompt
    session.session_id = "s2"  # another conversation asks again
    result, asked = _dispatch(tool, session, [t("tool.permission.deny")])
    assert not result.ok and len(asked) == 1


def _drain(app, until, timeout=5.0):
    import time

    end = time.time() + timeout
    while time.time() < end and not until():
        app.processEvents()
        time.sleep(0.01)


def test_cancel_stops_a_running_request_and_reports_it():
    app = QApplication.instance() or QApplication([])
    started = threading.Event()

    async def handler(request, emit):
        await emit(AgentEvent(request_id=request.id, type=EventType.TOKEN, text="partial"))
        started.set()
        await asyncio.sleep(30)  # a long model call or tool
        await emit(AgentEvent(request_id=request.id, type=EventType.DONE))

    bus = EventBus(handler)
    tracker = RunTracker(bus)
    seen = []
    bus.event.connect(seen.append)
    request = AppRequest(prompt="long job")
    tracker.start(request.id, request.prompt)
    bus.submit(request)
    assert started.wait(5)
    _drain(app, lambda: tracker.get(request.id).text == "partial")
    assert tracker.latest_running().request_id == request.id  # a reopened dialog finds it

    bus.cancel(request.id)
    _drain(app, lambda: any(e.type == EventType.DONE for e in seen))
    done = [e for e in seen if e.type == EventType.DONE]
    assert done and done[0].meta.get("cancelled")
    run = tracker.get(request.id)
    assert not run.running and run.cancelled and run.text == "partial"
    assert tracker.latest_running() is None
    bus.shutdown()


class _FakeStorage:
    def list_model_configs(self):
        return [{"display_name": "m"}]


def test_reopened_dialog_follows_the_running_task_and_can_stop_it():
    from PySide6.QtCore import QObject, Signal


    from pillow_assistant.ui.quick_input import QuickInputBar

    app = QApplication.instance() or QApplication([])

    class FakeBus(QObject):
        event = Signal(object)

        def __init__(self):
            super().__init__()
            self.cancelled = []
            self.submitted = []

        def submit(self, request):
            self.submitted.append(request)

        def cancel(self, request_id):
            self.cancelled.append(request_id)

    bus = FakeBus()
    tracker = RunTracker(bus)
    session = Session()
    first = QuickInputBar(_FakeStorage(), bus, session, tracker=tracker)
    first.prompt_edit.setText("整理表格")
    first._on_submit()
    rid = bus.submitted[0].id
    bus.event.emit(AgentEvent(request_id=rid, type=EventType.TOKEN, text="已读入 120 行"))
    first.close()  # the user closes the dialog; the run goes on

    second = QuickInputBar(_FakeStorage(), bus, session, tracker=tracker)
    assert second._active_id == rid
    assert "整理表格" in second.response_view.toPlainText()
    assert "已读入 120 行" in second.response_view.toPlainText()
    bus.event.emit(AgentEvent(request_id=rid, type=EventType.TOKEN, text="，正在改字体"))
    assert "正在改字体" in second.response_view.toPlainText()
    assert not second.stop_btn.isHidden()
    second._on_stop()
    assert bus.cancelled == [rid]
    bus.event.emit(AgentEvent(request_id=rid, type=EventType.DONE, meta={"cancelled": True}))
    assert second._active_id is None and second.stop_btn.isHidden()
    assert t("input.stopped") in second.response_view.toPlainText()
    second.close()
    app.processEvents()


def test_last_thing_in_the_context_is_this_turns_request_and_its_attachments(tmp_path):
    import pillow_assistant.core.orchestrator as orch
    from pillow_assistant.core import llm
    from pillow_assistant.core.project_manager import ProjectManager
    from pillow_assistant.core.triage import TriageResult
    from storage.projects import ProjectStore

    sheet = tmp_path / "表.xlsx"
    sheet.write_bytes(b"PK")
    captured = {}

    async def fake_triage(prompt, index, *, cfg, api_key, current_id=None, **_):
        return TriageResult(action="continue", project_id=index[0]["id"], confidence=0.95)

    async def fake_cwt(**kwargs):
        captured["user"] = kwargs["messages"][-1]["content"]
        return llm.ToolTurn(content="ok", tool_calls=[])

    class FakeStorage:
        def get_model_config(self, ref):
            return {"display_name": "m", "provider": "OpenAI", "model": "x", "base_url": None, "extra": None}

    store = ProjectStore(tmp_path / "projects")
    project = store.create("营业部格式修改")
    session = Session()
    session.project_id, session.session_id = project.id, store.new_session_id()
    store.record_turn(project, session.session_id, "1.将淮南中支营业部改为中支营业部", "当前项目为空目录")
    orig = orch.triage, llm.complete_with_tools
    orch.triage, llm.complete_with_tools = fake_triage, fake_cwt
    try:
        async def emit(_ev):
            pass

        request = AppRequest(prompt="合肥的顺序调为瑶海、肥西", model_ref="m", references=[str(sheet)])
        asyncio.run(orch.Orchestrator(FakeStorage(), None, ProjectManager(store, session))(request, emit))
    finally:
        orch.triage, llm.complete_with_tools = orig
    text = captured["user"] if isinstance(captured["user"], str) else captured["user"][0]["text"]
    from pillow_assistant.core.context_budget import REQUEST_OPEN
    background, _, request_part = text.rpartition(REQUEST_OPEN)
    assert t("core.focus_request") in background
    assert background.split("</pillow_supporting_context>")[0].rstrip().endswith(f"- {sheet}")  # last background lines
    assert "合肥的顺序" in request_part


def test_a_question_in_another_project_leaves_the_old_files_behind(tmp_path):
    import pillow_assistant.core.orchestrator as orch
    from pillow_assistant.core import llm
    from pillow_assistant.core.project_manager import ProjectManager
    from pillow_assistant.core.triage import TriageResult
    from storage.projects import ProjectStore

    old_sheet, new_step = tmp_path / "表.xlsx", tmp_path / "plate.step"
    old_sheet.write_bytes(b"PK")
    new_step.write_text("ISO-10303-21;", "utf-8")
    captured = {}

    async def fake_triage(prompt, index, *, cfg, api_key, current_id=None, **_):
        return TriageResult(action="new", name="电机安装板设计", confidence=0.95)

    async def fake_cwt(**kwargs):
        captured["user"] = kwargs["messages"][-1]["content"]
        return llm.ToolTurn(content="ok", tool_calls=[])

    class FakeStorage:
        def get_model_config(self, ref):
            return {"display_name": "m", "provider": "OpenAI", "model": "x", "base_url": None, "extra": None}

    store = ProjectStore(tmp_path / "projects")
    old = store.create("营业部格式修改")
    session = Session()
    session.project_id, session.session_id = old.id, store.new_session_id()
    session.add_reference(old_sheet)
    session.take_new(session.request_references())  # sent with the Excel question
    session.add_reference(new_step)  # dropped in for the new question
    refs = session.request_references()
    request = AppRequest(prompt="设计一块安装板", model_ref="m", references=refs,
                         meta={"new_references": session.take_new(refs)})
    notes = []

    async def emit(ev):
        if ev.type == EventType.TOKEN:
            notes.append(ev.text)

    orig = orch.triage, llm.complete_with_tools
    orch.triage, llm.complete_with_tools = fake_triage, fake_cwt
    try:
        asyncio.run(orch.Orchestrator(FakeStorage(), None, ProjectManager(store, session))(request, emit))
    finally:
        orch.triage, llm.complete_with_tools = orig
    assert request.references == [str(new_step)]
    assert session.references == [str(new_step)]  # the old chip is gone
    assert any("表.xlsx" in n for n in notes)
    assert str(old_sheet) not in captured["user"] and str(new_step) in captured["user"]


def test_deleting_a_project_removes_read_only_files(tmp_path):
    import stat as _stat

    from storage.projects import ProjectStore

    store = ProjectStore(tmp_path / "projects")
    project = store.create("营业部格式修改")
    sheet = project.workspace / "存量协保员总表.xlsx"
    sheet.write_bytes(b"PK")
    sheet.chmod(_stat.S_IREAD)  # as copied from OneDrive
    assert store.delete(project.id)
    assert not project.root.exists()


def test_a_short_reply_keeps_the_previous_turn_in_view(tmp_path):
    """"已经关闭了啊" after "please close the file" must finish the 合肥 task, not drop it."""
    import pillow_assistant.core.orchestrator as orch
    from pillow_assistant.core import llm
    from pillow_assistant.core.project_manager import ProjectManager
    from pillow_assistant.core.triage import TriageResult
    from storage.projects import ProjectStore

    sheet = tmp_path / "表.xlsx"
    sheet.write_bytes(b"PK")
    captured = {}

    async def fake_triage(prompt, index, *, cfg, api_key, current_id=None, **_):
        return TriageResult(action="chat", confidence=0.9)

    async def fake_cwt(**kwargs):
        captured["messages"] = kwargs["messages"]
        return llm.ToolTurn(content="ok", tool_calls=[])

    class FakeStorage:
        def get_model_config(self, ref):
            return {"display_name": "m", "provider": "OpenAI", "model": "x", "base_url": None, "extra": None}

    orig = orch.triage, llm.complete_with_tools
    orch.triage, llm.complete_with_tools = fake_triage, fake_cwt
    try:
        o = orch.Orchestrator(FakeStorage(), None, ProjectManager(ProjectStore(tmp_path / "p"), Session()))
        o._chat_history = [{"role": "user", "content": "合肥的顺序调为瑶海、肥西"},
                           {"role": "assistant", "content": "保存失败，请先关闭 Excel，我再重新执行。"}]

        async def emit(_ev):
            pass

        asyncio.run(o(AppRequest(prompt="已经关闭了啊", model_ref="m", references=[str(sheet)]), emit))
    finally:
        orch.triage, llm.complete_with_tools = orig
    contents = [m["content"] if isinstance(m["content"], str) else m["content"][0]["text"]
                for m in captured["messages"]]
    assert any("合肥的顺序" in c for c in contents[:-1])  # the earlier turn is in the conversation
    last = contents[-1]
    assert t("core.focus_request") in last and "已经关了" in t("core.focus_request")
def test_sandbox_output_with_characters_outside_the_locale_codec(tmp_path):
    from pillow_assistant.core.tools.sandbox import Sandbox

    result = Sandbox(tmp_path).run_python('print("存量协保员总表_备份.xlsx ✅ 😀 é")')
    assert result.returncode == 0, result.stderr
    assert "存量协保员总表_备份.xlsx ✅ 😀 é" in result.stdout


def test_triage_sees_the_project_of_a_session_without_attachments(tmp_path):
    import pillow_assistant.core.orchestrator as orch
    from pillow_assistant.core import llm
    from pillow_assistant.core.project_manager import ProjectManager
    from storage.projects import ProjectStore

    store = ProjectStore(tmp_path / "projects")
    plate = store.create("步进电机安装板")
    store.create("桌面手机支架")  # updated more recently
    session = Session()  # no chips: len(session) == 0
    session.project_id, session.session_id = plate.id, store.new_session_id()

    async def fake_cwt(**kwargs):
        return llm.ToolTurn(content="ok", tool_calls=[])

    class FakeStorage:
        def get_model_config(self, ref):
            return {"display_name": "m", "provider": "OpenAI", "model": "x", "base_url": None, "extra": None}

    seen = {}

    async def classifier(**kwargs):
        seen["user"] = kwargs["messages"][-1]["content"]
        return '{"action":"continue","project_id":"%s","confidence":0.9}' % plate.id

    orig = llm.complete_with_tools, llm.complete
    llm.complete_with_tools, llm.complete = fake_cwt, classifier
    try:
        async def emit(_ev):
            pass

        asyncio.run(orch.Orchestrator(FakeStorage(), None, ProjectManager(store, session))(
            AppRequest(prompt="展示一下三视图", model_ref="m"), emit))
    finally:
        llm.complete_with_tools, llm.complete = orig
    assert f"当前项目：id={plate.id}" in seen["user"]
    assert session.project_id == plate.id
    import json
    rows = [json.loads(line) for line in (plate.root / "audit.jsonl").read_text("utf-8").splitlines()]
    assert {"kind": "triage", "from_project": plate.id, "action": "continue"}.items() <= \
        next(r for r in rows if r["kind"] == "triage").items()


def test_dialog_returns_next_to_the_button_when_the_displays_close():
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QWidget

    from pillow_assistant.ui.window_coordinator import WindowCoordinator

    app = QApplication.instance() or QApplication([])
    anchor = QWidget()
    anchor.setGeometry(100, 100, 48, 48)
    anchor.show()
    coordinator = WindowCoordinator(anchor, None)
    bar = QWidget()
    bar.show()
    coordinator.set_window("quick", bar)
    tiles = [QWidget(), QWidget()]
    for tile in tiles:
        tile.show()
        coordinator.add_multi_window(tile)
    bar.move(QPoint(700, 900))  # parked under the tiles
    coordinator.park_quick_for_displays()

    coordinator.remove_multi_window(tiles[0])
    assert bar.pos() == QPoint(700, 900)  # one display still open
    coordinator.remove_multi_window(tiles[1])
    assert bar.pos() == coordinator.anchor_point()

    bar.move(QPoint(700, 900))  # not parked: closing displays leaves it alone
    coordinator.add_multi_window(tiles[0])
    coordinator.close_multi_windows()
    assert bar.pos() == QPoint(700, 900)
    for widget in (bar, anchor):
        widget.close()
    app.processEvents()


def test_closing_one_preview_tile_takes_its_file_off_the_question():
    from PySide6.QtWidgets import QWidget

    from pillow_assistant.ui.window_coordinator import WindowCoordinator

    app = QApplication.instance() or QApplication([])
    session = Session()
    session.set_displayed(["C:/w/review.png", "C:/w/front.png"])
    coordinator = WindowCoordinator(QWidget(), None, session=session)
    tile = QWidget()
    tile.shown_path = "C:/w/review.png"
    coordinator.add_multi_window(tile)
    coordinator.remove_multi_window(tile)
    assert [p.replace("\\", "/") for p in session.displayed] == ["C:/w/front.png"]
    app.processEvents()


def test_a_recording_ends_when_the_button_is_no_longer_held(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QPushButton, QWidget

    from pillow_assistant.ui.gesture_controller import GestureController, GestureState

    app = QApplication.instance() or QApplication([])
    anchor = QWidget()
    gestures = GestureController(QPushButton(anchor), anchor)
    ended = []
    gestures.recording_finished.connect(lambda: ended.append(True))
    gestures.state = GestureState.PRESSED
    gestures._begin_recording()
    monkeypatch.setattr(QGuiApplication, "mouseButtons", staticmethod(lambda: Qt.NoButton))
    gestures._check_still_held()  # the release never arrived
    assert ended == [True] and gestures.state == GestureState.IDLE
    app.processEvents()
