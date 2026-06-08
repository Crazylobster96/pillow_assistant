"""Tests for the present_windows tool (multi-window tiled display)."""

import asyncio
from pathlib import Path

from pillow_assistant.contracts import EventType, SurfaceLevel
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.present_tool import PresentTool


def _ctx(events, tmp_path, request_id="req1"):
    async def emit(ev):
        events.append(ev)
    return ToolContext(workspace=Path(tmp_path), emit=emit, request_id=request_id)


def test_text_items_row(tmp_path):
    events = []
    t = PresentTool()
    r = asyncio.run(t({"items": [{"title": "A", "text": "aaa"}, {"title": "B", "text": "bbb"}],
                       "layout": "row"}, _ctx(events, tmp_path)))
    assert r.ok
    ev = events[-1]
    assert ev.type == EventType.SURFACE
    assert ev.surface.kind == "multi" and ev.surface.level == SurfaceLevel.L5
    assert ev.surface.payload["layout"] == "row"
    assert len(ev.surface.payload["views"]) == 2
    assert ev.request_id == "req1"


def test_path_item_and_missing_path_skipped(tmp_path):
    events = []
    t = PresentTool()
    p = tmp_path / "x.txt"
    p.write_text("hi")
    r = asyncio.run(t({"items": [{"path": str(p)}, {"path": str(tmp_path / "nope.txt")},
                                 {"title": "t", "text": "y"}],
                       "layout": "column"}, _ctx(events, tmp_path)))
    assert r.ok and "跳过" in r.text
    views = events[-1].surface.payload["views"]
    assert len(views) == 2
    assert views[0]["path"] == str(p) and views[0]["title"] == "x.txt"
    assert events[-1].surface.payload["layout"] == "column"


def test_empty_items_fails(tmp_path):
    r = asyncio.run(PresentTool()({"items": []}, _ctx([], tmp_path)))
    assert not r.ok


def test_bad_layout_falls_back_to_row(tmp_path):
    events = []
    r = asyncio.run(PresentTool()({"items": [{"text": "z"}], "layout": "diagonal"},
                                  _ctx(events, tmp_path)))
    assert r.ok and events[-1].surface.payload["layout"] == "row"


def test_no_emit_fails(tmp_path):
    ctx = ToolContext(workspace=Path(tmp_path))  # no UI attached
    r = asyncio.run(PresentTool()({"items": [{"text": "z"}]}, ctx))
    assert not r.ok


def test_cap_at_four_windows(tmp_path):
    events = []
    r = asyncio.run(PresentTool()({"items": [{"text": str(i)} for i in range(6)]},
                                  _ctx(events, tmp_path)))
    assert r.ok
    assert len(events[-1].surface.payload["views"]) == 4
