"""Triage reads the request against the current project and its last exchange."""

import asyncio

from pillow_assistant.core import llm
from pillow_assistant.core import triage as triage_mod

INDEX = [{"id": "p1", "name": "协保员表", "last_prompt": ""}, {"id": "p2", "name": "周报", "last_prompt": ""}]


def test_triage_sees_the_current_project_and_its_last_exchange(monkeypatch):
    seen = {}

    async def classifier(**kwargs):
        seen["user"] = kwargs["messages"][-1]["content"]
        return '{"object":"协保员表","action":"continue","project_id":"p1","confidence":0.9}'

    monkeypatch.setattr(llm, "complete", classifier)
    result = asyncio.run(triage_mod.triage("再加一列", INDEX, cfg={}, api_key=None, current_id="p1",
                                           current_context="user: 把合肥的顺序调一下"))
    assert (result.action, result.project_id) == ("continue", "p1")
    assert 'id=p1 名称="协保员表"' in seen["user"] and "把合肥的顺序调一下" in seen["user"]

    async def broken(**kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(llm, "complete", broken)
    result = asyncio.run(triage_mod.triage("再加一列", INDEX, cfg={}, api_key=None, current_id="p1"))
    assert (result.action, result.project_id) == ("continue", "p1")


def test_a_keyword_first_pass_goes_to_the_model_which_may_override_it(monkeypatch):
    guess = lambda text: triage_mod.prejudge(text, INDEX, INDEX[0])["guess"]  # noqa: E731
    assert guess("再加一列") == "延续当前项目「协保员表」"
    assert guess("周报再补一段") == "延续「周报」"
    assert guess("做一个新的排班表") == "新建项目"
    assert guess("Excel 怎么冻结首行？") == "闲聊（一问一答）"
    seen = {}

    async def classifier(**kwargs):
        seen["user"] = kwargs["messages"][-1]["content"]
        return '{"action":"chat","prejudge_ok":false,"confidence":0.9}'

    monkeypatch.setattr(llm, "complete", classifier)
    result = asyncio.run(triage_mod.triage("再加一列", INDEX, cfg={}, api_key=None, current_id="p1"))
    assert "关键词初判：延续当前项目「协保员表」" in seen["user"] and result.prejudge_ok is False


def test_without_the_model_the_keyword_first_pass_decides(monkeypatch):
    async def broken(**kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(llm, "complete", broken)
    run = lambda text: asyncio.run(triage_mod.triage(text, INDEX, cfg={}, api_key=None, current_id="p1"))  # noqa: E731
    assert run("周报再补一段").project_id == "p2"
    assert run("做一个新的排班表").action == "new"
    assert run("Excel 怎么冻结首行？").action == "chat"
    assert run("再加一列").project_id == "p1"
