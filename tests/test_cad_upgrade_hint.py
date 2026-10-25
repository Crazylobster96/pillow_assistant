"""Standard edition: CAD / concept-diagram talk suggests the text-to-cad-anatomy edition."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core import upgrade_hint
from pillow_assistant.core.cad_intent import mentions_cad


def test_mentions_cad():
    for text in ("帮我做一个齿轮箱的3D模型", "画一张锁芯的概念图", "导出 STEP 文件", "open part.stl",
                 "make an exploded view", "CNC toolpath", "what is CAD?"):
        assert mentions_cad(text), text
    for text in ("数据建模怎么做", "列表切片", "打开相机 camera", "写个python脚本", ""):
        assert not mentions_cad(text), text


def test_hint_once_per_run(monkeypatch):
    monkeypatch.setattr(upgrade_hint, "has_cad_edition", lambda: False)
    upgrade_hint.reset()
    assert upgrade_hint.cad_upgrade_hint("写个python脚本") is None
    hint = upgrade_hint.cad_upgrade_hint("画一张锁芯的概念图")
    assert hint and upgrade_hint.RELEASES_URL in hint and "text-to-cad-anatomy" in hint
    assert upgrade_hint.cad_upgrade_hint("再画一张 CAD 图") is None
    upgrade_hint.reset()


def test_no_hint_in_cad_edition(monkeypatch):
    monkeypatch.setattr(upgrade_hint, "has_cad_edition", lambda: True)
    upgrade_hint.reset()
    assert upgrade_hint.cad_upgrade_hint("帮我做一个齿轮箱的3D模型") is None
    upgrade_hint.reset()


def test_edition_detection_matches_this_checkout():
    has_cad = (ROOT / "pillow_assistant" / "capabilities" / "text_to_cad.py").exists()
    assert upgrade_hint.has_cad_edition() == has_cad
