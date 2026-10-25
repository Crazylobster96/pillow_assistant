"""CAD project routing (text-to-cad edition): CAD requests are triaged on their
own, and the user can name or pick the project in the message."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core import llm, triage as triage_mod
from pillow_assistant.core.cad_intent import mentions_cad
from pillow_assistant.core.project_directive import parse_project_directive, resolve_directive
from pillow_assistant.core.tools.builtin.project_tools import RenameProjectTool
from storage.projects import ProjectStore

INDEX = [{"id": "p1", "name": "齿轮箱", "last_prompt": ""}, {"id": "p2", "name": "Rover", "last_prompt": ""}]


def test_mentions_cad():
    for text in ("帮我做一个齿轮箱的3D模型", "画一张锁芯的概念图", "导出 STEP 文件", "open part.stl",
                 "把齿轮建模一下", "make an exploded view", "CNC toolpath", "what is CAD?"):
        assert mentions_cad(text), text
    for text in ("数据建模怎么做", "列表切片", "打开相机 camera", "写个python脚本", ""):
        assert not mentions_cad(text), text


def test_parse_project_directive():
    cases = {
        "项目：齿轮箱 帮我做个外壳": ("assign", "齿轮箱"),
        "项目：齿轮箱 v2": ("assign", "齿轮箱 v2"),
        "项目名为 无人机": ("assign", "无人机"),
        "把这个归入「齿轮箱」项目": ("join", "齿轮箱"),
        "新建项目「齿轮箱 v2」并建模": ("new", "齿轮箱 v2"),
        "新建一个项目叫无人机机架": ("new", "无人机机架"),
        "新建项目：台灯 做底座": ("new", "台灯"),
        "新建项目做个机械臂": ("new", ""),
        "project: gear box, make a lid": ("assign", "gear box"),
        "put this in the rover project": ("join", "rover"),
        "new project called rover": ("new", "rover"),
    }
    for text, expected in cases.items():
        d = parse_project_directive(text)
        assert d is not None and (d.mode, d.name) == expected, (text, d)
    for text in ("把项目改名为 X", "这个项目：需要三步", "放到这个项目", "打开项目列表",
                 "写一个创建项目的脚本", "做个3D模型", "in this project"):
        assert parse_project_directive(text) is None, text


def test_resolve_directive():
    assert resolve_directive(parse_project_directive("项目：齿轮箱"), INDEX) == ("continue", "p1", "齿轮箱")
    assert resolve_directive(parse_project_directive("project: rover"), INDEX) == ("continue", "p2", "Rover")
    assert resolve_directive(parse_project_directive("项目：台灯"), INDEX) == ("new", None, "台灯")
    assert resolve_directive(parse_project_directive("新建项目「齿轮箱」"), INDEX) == ("new", None, "齿轮箱")
    # Loose phrasing only joins an existing project.
    assert resolve_directive(parse_project_directive("what tool for the CAD project"), INDEX) is None


def _run_triage(prompt, reply, current_id=None):
    seen = {}

    async def fake_complete(**kwargs):
        seen["messages"] = kwargs["messages"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    original = llm.complete
    llm.complete = fake_complete
    try:
        result = asyncio.run(triage_mod.triage(prompt, INDEX, cfg={}, api_key=None, current_id=current_id))
    finally:
        llm.complete = original
    return result, seen.get("messages")


def test_user_project_skips_the_model():
    result, messages = _run_triage("项目：齿轮箱 加一个安装孔", "{}", current_id="p2")
    assert (result.action, result.project_id, result.rationale) == ("continue", "p1", "user-project")
    assert messages is None
    result, _ = _run_triage("新建项目「台灯」画个底座的概念图", "{}")
    assert (result.action, result.name, result.confidence) == ("new", "台灯", 1.0)


def test_unnamed_new_project_is_always_new():
    result, _ = _run_triage("新建项目，做个机械臂", '{"action":"continue","project_id":"p1","confidence":0.9}')
    assert result.action == "new" and result.name
    result, _ = _run_triage("新建项目做个机械臂底座", '{"action":"new","name":"机械臂底座","confidence":0.9}')
    assert (result.action, result.name) == ("new", "机械臂底座")
    result, _ = _run_triage("新建项目做个机械臂", RuntimeError("offline"))
    assert result.action == "new"


def test_cad_request_is_judged_independently():
    _, messages = _run_triage("给齿轮箱做一个 STEP 模型", '{"action":"new","name":"齿轮箱","confidence":0.9}',
                              current_id="p2")
    context = messages[-1]["content"]
    assert "倾向 continue" not in context
    assert "CAD" in context
    _, messages = _run_triage("把表格再加一列", '{"action":"continue","project_id":"p2","confidence":0.9}',
                              current_id="p2")
    assert "倾向 continue" in messages[-1]["content"]


def test_rename_project_tool(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create("零件")
    other = store.create("其他")
    ctx = SimpleNamespace(project_store=store, session=SimpleNamespace(project_id=project.id))
    tool = RenameProjectTool()
    result = asyncio.run(tool({"new_name": "齿轮箱外壳"}, ctx))
    assert result.ok and store.get(project.id).name == "齿轮箱外壳"
    result = asyncio.run(tool({"new_name": "杂项", "project": "其他"}, ctx))
    assert result.ok and store.get(other.id).name == "杂项"
    assert not asyncio.run(tool({"new_name": ""}, ctx)).ok
    ctx.session.project_id = None
    assert not asyncio.run(tool({"new_name": "x"}, ctx)).ok


def test_parse_inherit_source():
    from pillow_assistant.core.project_directive import parse_inherit_source

    cases = {
        "基于「齿轮箱」项目做CNC刀路": "齿轮箱",
        "用齿轮箱项目的模型画个爆炸概念图": "齿轮箱",
        "在齿轮箱项目的成果上做 CAM": "齿轮箱",
        "make toolpaths based on the gearbox project": "gearbox",
        "build on project rover": "rover",
    }
    for text, name in cases.items():
        assert parse_inherit_source(text) == name, text
    for text in ("基于这个项目", "做个齿轮箱", "接着齿轮箱项目做"):
        assert parse_inherit_source(text) is None, text


def test_triage_inherits_from_named_project():
    result, messages = _run_triage("项目：齿轮箱 CAM，基于「齿轮箱」项目做刀路", "{}")
    assert (result.action, result.name, result.inherit_from) == ("new", "齿轮箱 CAM", "p1")
    assert messages is None
    result, _ = _run_triage("基于齿轮箱项目做一个加长版", '{"action":"new","name":"加长齿轮箱","confidence":0.9}')
    assert (result.action, result.name, result.inherit_from) == ("new", "加长齿轮箱", "p1")
    # The model picked the source itself: "基于" still means a new project.
    result, _ = _run_triage("基于齿轮箱项目做一个加长版", '{"action":"continue","project_id":"p1","confidence":0.9}')
    assert result.action == "new" and result.inherit_from == "p1"
    # Building on it inside another existing project.
    result, _ = _run_triage("用齿轮箱项目的模型给 Rover 装上", '{"action":"continue","project_id":"p2","confidence":0.9}')
    assert (result.action, result.project_id, result.inherit_from) == ("continue", "p2", "p1")
    # The model may ask for inheritance on its own.
    result, _ = _run_triage("做个齿轮箱 v2 的 3D 模型",
                            '{"action":"new","name":"齿轮箱 v2","inherit_from":"p1","confidence":0.9}')
    assert result.inherit_from == "p1"
    result, _ = _run_triage("做个齿轮箱 v2 的 3D 模型",
                            '{"action":"new","name":"齿轮箱 v2","inherit_from":"ghost","confidence":0.9}')
    assert result.inherit_from is None


def _cad_workspace(store):
    cad = store.create("齿轮箱")
    (cad.workspace / "gearbox.step").write_text("STEP")
    (cad.workspace / "models").mkdir()
    (cad.workspace / "models" / "gearbox.py").write_text("print(1)")
    for skipped in ("anatomy/kit/iso-kit.mjs", "anatomy/scripts/capture.mjs", "__pycache__/x.pyc", ".cache/y"):
        (cad.workspace / skipped).parent.mkdir(parents=True, exist_ok=True)
        (cad.workspace / skipped).write_text("x")
    (cad.workspace / "anatomy" / "figures" / "lock").mkdir(parents=True)
    (cad.workspace / "anatomy" / "figures" / "lock" / "build.mjs").write_text("fig")
    return cad


def test_store_inherit_copies_outputs(tmp_path):
    store = ProjectStore(tmp_path)
    cad = _cad_workspace(store)
    cam = store.create("齿轮箱 CAM")
    (cam.workspace / "gearbox.step").write_text("MINE")
    copied = store.inherit(cam, cad)
    assert sorted(copied) == ["anatomy/figures/lock/build.mjs", "models/gearbox.py"]
    assert (cam.workspace / "gearbox.step").read_text() == "MINE"  # never overwritten
    (cam.workspace / "models" / "gearbox.py").write_text("changed")
    assert (cad.workspace / "models" / "gearbox.py").read_text() == "print(1)"  # a copy, not a link
    again = store.get(cam.id)
    assert [(e["id"], e["name"], e["files"]) for e in again.inherited] == [(cad.id, "齿轮箱", copied)]


def test_project_manager_applies_inheritance(tmp_path):
    from pillow_assistant.core.project_manager import ProjectManager
    from pillow_assistant.core.session import Session
    from pillow_assistant.core.triage import TriageResult

    store = ProjectStore(tmp_path)
    cad = _cad_workspace(store)
    pm = ProjectManager(store, Session())
    project = pm.apply(TriageResult(action="new", name="齿轮箱演示图", inherit_from=cad.id), "x")
    assert (project.workspace / "gearbox.step").is_file()
    assert project.inherited[0]["id"] == cad.id


def test_inherit_project_tool(tmp_path):
    from pillow_assistant.core.tools.builtin.project_tools import InheritProjectTool

    store = ProjectStore(tmp_path)
    cad = _cad_workspace(store)
    demo = store.create("演示")
    ctx = SimpleNamespace(project_store=store, session=SimpleNamespace(project_id=demo.id))
    tool = InheritProjectTool()
    result = asyncio.run(tool({"project": "齿轮箱"}, ctx))
    assert result.ok and (demo.workspace / "gearbox.step").is_file()
    assert not asyncio.run(tool({"project": "演示"}, ctx)).ok
    ctx.session.project_id = None
    assert not asyncio.run(tool({"project": "齿轮箱"}, ctx)).ok


def _lineage_store(tmp_path):
    store = ProjectStore(tmp_path)
    cad = _cad_workspace(store)
    cam = store.create("齿轮箱 CAM")
    store.inherit(cam, cad)
    v2 = store.create("齿轮箱 v2")
    store.inherit(v2, cad)
    v2cam = store.create("齿轮箱 v2 CAM")
    store.inherit(v2cam, v2)
    store.create("无关项目")
    return store, cad, cam, v2, v2cam


def test_lineage_graph_and_text(tmp_path):
    from pillow_assistant.core import project_lineage as lineage

    store, cad, cam, v2, v2cam = _lineage_store(tmp_path)
    graph = lineage.build_graph(store)
    assert len(graph["nodes"]) == 5 and len(graph["edges"]) == 3
    assert [n["name"] for n in lineage.parents(graph, v2cam.id)] == ["齿轮箱 v2"]
    assert sorted(n["name"] for n in lineage.children(graph, cad.id)) == ["齿轮箱 CAM", "齿轮箱 v2"]
    part = lineage.component(graph, v2cam.id)
    assert set(part["nodes"]) == {cad.id, cam.id, v2.id, v2cam.id}  # the unrelated project is left out
    text = lineage.render_text(part, v2cam.id)
    assert text.splitlines()[0].startswith("齿轮箱 ") and "◀" in text and "无关项目" not in text
    svg = lineage.render_svg(part, v2cam.id)
    assert svg.startswith("<svg") and svg.count("<rect") == 5 and "齿轮箱 v2 CAM" in svg
    assert "<svg" in lineage.render_html(part, v2cam.id)
    lonely = lineage.component(graph, store.find_by_name("无关项目")[0].id)
    assert not lonely["edges"] and lineage.render_text(lonely) == lineage.render_text({"nodes": {}, "edges": []})


def test_lineage_keeps_deleted_parents(tmp_path):
    from pillow_assistant.core import project_lineage as lineage

    store, cad, cam, v2, v2cam = _lineage_store(tmp_path)
    assert store.delete(cad.id)
    graph = lineage.build_graph(store)
    assert graph["nodes"][cad.id]["deleted"] and graph["nodes"][cad.id]["name"] == "齿轮箱"
    assert len(lineage.component(graph, cam.id)["edges"]) == 3


def test_project_lineage_tool(tmp_path, monkeypatch):
    import webbrowser

    from pillow_assistant.core.tools.builtin.project_tools import ProjectLineageTool

    store, cad, cam, v2, v2cam = _lineage_store(tmp_path)
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    ctx = SimpleNamespace(project_store=store, session=SimpleNamespace(project_id=v2.id))
    tool = ProjectLineageTool()
    result = asyncio.run(tool({}, ctx))
    assert result.ok and "齿轮箱 v2 CAM" in result.text and not opened
    result = asyncio.run(tool({"project": "齿轮箱 CAM", "open": True}, ctx))
    assert result.ok and opened and (tmp_path / "lineage.html").is_file()
    assert asyncio.run(tool({"project": "all"}, ctx)).ok
    assert not asyncio.run(tool({"project": "不存在"}, ctx)).ok
    assert len(store.list()) == 5  # lineage.html beside the project folders is not a project
