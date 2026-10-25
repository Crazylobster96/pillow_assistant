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
