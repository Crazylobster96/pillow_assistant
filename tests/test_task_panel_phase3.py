"""Phase 3: tasks carry their project, other projects are marked and can be switched to, a task
waiting for the user goes first, the panel can live on as its own window, and a finished task
the user has seen leaves the tabs."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QObject, Signal  # noqa: E402

from pillow_assistant.contracts import AgentEvent, EventType  # noqa: E402
from pillow_assistant.ui.task_panel import TaskPanel  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class Bus(QObject):
    event = Signal(object)

    def tell(self, rid, text):
        pass


def test_a_task_in_another_project_is_marked_and_offers_the_switch(app):
    bus = Bus()
    switched = []
    panel = TaskPanel(bus=bus, current_project=lambda: "p-here", on_switch=switched.append)
    page = panel.add("r1", "出铣削程序")
    bus.event.emit(AgentEvent(request_id="r1", type=EventType.TOKEN, text="（继续项目：安装板）\n",
                              meta={"project": "安装板", "project_id": "p-there"}))
    assert page.project == "安装板" and page.other_project and not page.switch_btn.isHidden()
    assert "↗" in panel._tabs[0].sub.text()
    page.switch_btn.click()
    assert switched == ["p-there"]
    panel.close()


def test_a_task_waiting_for_the_user_goes_first(app):
    bus = Bus()
    panel = TaskPanel(bus=bus)
    for rid in ("a", "b", "c"):
        panel.add(rid, rid).set_step("running")
    bus.event.emit(AgentEvent(request_id="a", type=EventType.ASK, text="允许删除吗？"))
    assert panel.page("a").waiting and panel._tabs[0].page.request_id == "a"
    bus.event.emit(AgentEvent(request_id="a", type=EventType.TOOL_RESULT, text="ok", meta={"ok": True}))
    assert not panel.page("a").waiting
    panel.close()


def test_the_panel_follows_the_bus_and_outlives_a_drag(app):
    bus = Bus()
    panel = TaskPanel(bus=bus)
    page = panel.add("r", "整理下载")
    bus.event.emit(AgentEvent(request_id="r", type=EventType.TOKEN, text="已移动 12 个文件\n"))
    assert "已移动 12 个文件" in page.log.toPlainText()
    bus.event.emit(AgentEvent(request_id="r", type=EventType.DONE))
    assert page.state == "done"
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    docked = []
    panel.bind(on_dock=lambda: docked.append(True))
    panel.docked = False  # dragged out: a window of its own
    TaskPanel.detached = panel
    QTest.mouseDClick(panel, Qt.LeftButton)
    assert panel.docked and TaskPanel.detached is None and docked == [True]
    panel.close()


def test_a_seen_finished_task_is_archived(app):
    panel = TaskPanel()
    panel.ARCHIVE_AFTER_MS = 10
    page = panel.add("r", "合并 PDF")
    panel.add("s", "写周报").set_step("running")
    page.set_state("done")
    panel.select("r")  # looked at after it ended
    panel.select("s")
    import time
    end = time.time() + 2
    while time.time() < end and "r" in panel.pages:
        app.processEvents()
        time.sleep(0.01)
    assert "r" not in panel.pages and "s" in panel.pages
    panel.close()


def test_the_tracker_reports_a_finished_run_with_its_project():
    from pillow_assistant.ui.run_tracker import RunTracker

    tracker = RunTracker()
    ended = []
    tracker.finished.connect(ended.append)
    tracker.start("r", "出程序")
    tracker.on_event(AgentEvent(request_id="r", type=EventType.TOKEN, text="x",
                                meta={"project": "安装板", "project_id": "p1"}))
    tracker.on_event(AgentEvent(request_id="r", type=EventType.DONE))
    assert [(r.project, r.project_id) for r in ended] == [("安装板", "p1")]


def test_a_dragged_out_panel_can_be_closed_and_the_next_task_opens_a_new_one(app):
    from PySide6.QtCore import QCoreApplication, QEvent

    panel = TaskPanel()
    panel.add("r1", "做一块板")
    panel.show()
    panel.docked = False
    TaskPanel.detached = panel
    gone = []
    panel.destroyed.connect(lambda *_: gone.append(True))
    panel.close_btn.click()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert gone and TaskPanel.detached is None
