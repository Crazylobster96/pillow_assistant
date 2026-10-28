"""A short reply to the question an answer ended with stays in that project."""

from storage.projects import ProjectStore


def test_a_short_reply_to_the_answers_question_stays_in_the_project(tmp_path):
    from pillow_assistant.core.orchestrator import Orchestrator
    from pillow_assistant.core.project_manager import ProjectManager
    from pillow_assistant.core.session import Session
    from pillow_assistant.core.triage import TriageResult

    store = ProjectStore(tmp_path / "projects")
    plate = store.create("电机安装板")
    pm = ProjectManager(store, Session())
    pm.apply(TriageResult(action="continue", project_id=plate.id), "出铣削程序")
    sid = pm.session.session_id
    long_answer = "铣削程序已生成。" + "工序说明。" * 200 + "\n\n需要我打开预览图让您检查刀路吗？"
    store.append_history(plate, sid, "user", "出铣削程序")
    store.append_history(plate, sid, "assistant", long_answer)
    orch = Orchestrator.__new__(Orchestrator)
    orch.pm = pm
    assert orch._pending_question(plate.id) == "需要我打开预览图让您检查刀路吗？"
    assert "检查刀路吗" in orch._last_exchange(plate.id)  # the end of a long answer reaches triage
    store.append_history(plate, sid, "assistant", "已打开预览图。")
    assert orch._pending_question(plate.id) == ""
