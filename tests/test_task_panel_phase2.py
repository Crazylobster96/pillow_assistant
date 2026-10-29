"""Phase 2: several tasks at once, a limit with a queue, tabs, and telling a running task more."""

import asyncio
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType  # noqa: E402
from pillow_assistant.core import llm, run_control  # noqa: E402
from pillow_assistant.core.agent.loop import ToolLoopAgent  # noqa: E402
from pillow_assistant.core.bus import EventBus  # noqa: E402
from pillow_assistant.core.tools.base import ToolContext  # noqa: E402
from pillow_assistant.core.tools.registry import ToolRegistry  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _drain(app, until, timeout=5):
    end = time.time() + timeout
    while time.time() < end and not until():
        app.processEvents()
        time.sleep(0.01)


def test_beyond_the_limit_a_task_waits_and_is_told_so(app, monkeypatch):
    monkeypatch.setattr(run_control, "max_tasks", lambda: 1)
    release = threading.Event()
    started = []

    async def handler(request, emit):
        started.append(request.prompt)
        while not release.is_set():
            await asyncio.sleep(0.01)
        await emit(AgentEvent(request_id=request.id, type=EventType.DONE))

    bus = EventBus(handler)
    seen = []
    bus.event.connect(seen.append)
    first, second = AppRequest(prompt="first"), AppRequest(prompt="second")
    bus.submit(first)
    _drain(app, lambda: started == ["first"])
    bus.submit(second)
    _drain(app, lambda: any((e.meta or {}).get("queued") for e in seen))
    assert started == ["first"]  # the second waits for a slot
    assert any((e.meta or {}).get("queued") and e.request_id == second.id for e in seen)
    release.set()
    _drain(app, lambda: started == ["first", "second"])
    assert started == ["first", "second"]
    bus.shutdown()


def test_words_for_a_running_task_reach_it_before_its_next_step(monkeypatch, tmp_path):
    seen = []

    async def fake(**kwargs):
        seen.append([dict(m) for m in kwargs["messages"]])
        return llm.ToolTurn(content="好了")

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    run_control.tell("r9", "顺便把标题改成蓝色")

    async def emit(event):
        pass

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, ToolRegistry(), ToolContext(workspace=str(tmp_path)))
    asyncio.run(agent.run(prompt="做个报表", emit=emit, request_id="r9"))
    assert any("顺便把标题改成蓝色" in str(m.get("content")) for m in seen[0])
    assert run_control.take_messages("r9") == []


def test_tabs_show_five_tasks_running_ones_first_and_the_rest_under_more(app):
    from pillow_assistant.ui.task_panel import TaskPanel

    panel = TaskPanel()
    for i in range(7):
        page = panel.add(f"r{i}", f"任务 {i}", started=1000 + i)
        if i < 3:
            page.set_state("done")
        else:
            page.set_step("运行中")
    shown = [tab.page.request_id for tab in panel._tabs]
    assert len(shown) == 5 and set(shown[:4]) == {"r3", "r4", "r5", "r6"}
    assert len(panel._hidden) == 2 and not panel.more_btn.isHidden()
    panel.close()


def test_the_dialog_takes_new_requests_while_tasks_run(app):
    from PySide6.QtCore import QObject, Signal

    from pillow_assistant.core.session import Session
    from pillow_assistant.ui.quick_input import QuickInputBar
    from pillow_assistant.ui.run_tracker import RunTracker

    class Bus(QObject):
        event = Signal(object)

        def __init__(self):
            super().__init__()
            self.submitted, self.told = [], []

        def submit(self, request):
            self.submitted.append(request)

        def tell(self, rid, text):
            self.told.append((rid, text))

        def cancel(self, rid):
            pass

    class Storage:
        def list_model_configs(self):
            return [{"display_name": "m"}]

    bus = Bus()
    bar = QuickInputBar(Storage(), bus, Session(), tracker=RunTracker(bus))
    for prompt in ("分析销售表", "整理下载文件夹"):
        bar.prompt_edit.setText(prompt)
        bar._on_submit()
    assert len(bus.submitted) == 2  # the second went in while the first ran
    a, b = (r.id for r in bus.submitted)
    for rid in (a, b):
        bus.event.emit(AgentEvent(request_id=rid, type=EventType.TOOL_START, text="x",
                                  meta={"name": "x", "step": 1, "total": 9}))
    assert set(bar._task_panel.pages) == {a, b}
    page = bar._task_panel.page(a)
    page.say.setText("只看华东")
    page._tell()
    assert bus.told == [(a, "只看华东")]
    bus.event.emit(AgentEvent(request_id=a, type=EventType.DONE))
    assert bar._active_id == b and page.state == "done"
    bar.close()
