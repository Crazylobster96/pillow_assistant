"""Phase 1 of the task panel: the dialog keeps to the conversation, a task's process
goes to the page beside it, and the task can be paused, resumed and stopped."""

import asyncio
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pillow_assistant.contracts import AgentEvent, EventType  # noqa: E402
from pillow_assistant.core import llm, run_control  # noqa: E402
from pillow_assistant.core.agent.loop import ToolLoopAgent  # noqa: E402
from pillow_assistant.core.tools.base import ToolContext  # noqa: E402
from pillow_assistant.core.tools.registry import ToolRegistry  # noqa: E402


def test_the_answer_is_marked_for_the_dialog(monkeypatch, tmp_path):
    async def fake(**kwargs):
        return llm.ToolTurn(content="好了")

    monkeypatch.setattr(llm, "complete_with_tools", fake)
    out = []

    async def emit(event):
        out.append(event)

    agent = ToolLoopAgent({"provider": "openai", "model": "m"}, None, ToolRegistry(), ToolContext(workspace=str(tmp_path)))
    asyncio.run(agent.run(prompt="hi", emit=emit, request_id="r"))
    answers = [e for e in out if e.type == EventType.TOKEN and (e.meta or {}).get("answer")]
    assert [e.text for e in answers] == ["好了"]


def test_a_paused_request_waits_before_its_next_step():
    async def scenario():
        run_control.pause("r1")
        steps = []

        async def worker():
            await run_control.checkpoint("r1")
            steps.append("ran")

        task = asyncio.ensure_future(worker())
        await asyncio.sleep(0.05)
        held = list(steps)
        run_control.resume("r1")
        await asyncio.wait_for(task, 1)
        run_control.forget("r1")
        return held, steps

    held, steps = asyncio.run(scenario())
    assert held == [] and steps == ["ran"]


def test_the_page_sits_against_the_dialog():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QRect

    from pillow_assistant.ui.task_panel import beside

    area = QRect(0, 0, 1920, 1080)
    right = beside(QRect(100, 200, 600, 400), 420, area)
    assert (right.x(), right.y(), right.width(), right.height()) == (708, 200, 420, 400)
    left = beside(QRect(1400, 200, 500, 400), 420, area)
    assert left.x() == 1400 - 8 - 420  # no room on the right: the left side


def test_pause_resume_and_stop_reach_the_bus():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])  # noqa: F841
    from pillow_assistant.ui.task_panel import TaskPage

    class Bus:
        def __init__(self):
            self.calls = []

        def pause(self, rid):
            self.calls.append(("pause", rid))

        def resume(self, rid):
            self.calls.append(("resume", rid))

        def cancel(self, rid):
            self.calls.append(("cancel", rid))

    bus = Bus()
    panel = TaskPage("整理下载文件夹", bus=bus, request_id="r")
    panel.set_step("第 1/200 步 · file_list")
    assert panel.state == "running"
    panel._toggle_pause()
    assert panel.state == "paused" and bus.calls[-1] == ("pause", "r")
    panel._toggle_pause()
    assert panel.state == "running" and bus.calls[-1] == ("resume", "r")
    panel._toggle_pause()
    panel._stop()  # a paused task is released so the stop can land
    assert bus.calls[-2:] == [("resume", "r"), ("cancel", "r")]
    panel.set_state("done")
    assert panel.pause_btn.isHidden() and panel.stop_btn.isHidden()
    panel.close()
