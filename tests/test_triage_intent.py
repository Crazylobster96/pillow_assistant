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
