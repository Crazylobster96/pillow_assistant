"""Tests for the Surface router (R3 pragmatic subset: L1 / L4 / L5)."""

from pillow_assistant.contracts import SurfaceLevel
from pillow_assistant.core.surface_router import route


def test_short_text_is_l1():
    assert route("ok") == SurfaceLevel.L1
    assert route("已完成。") == SurfaceLevel.L1
    assert route("") == SurfaceLevel.L1
    assert route(None) == SurfaceLevel.L1


def test_medium_text_is_l4():
    assert route("x" * 41) == SurfaceLevel.L4
    assert route("这是一段中等长度的普通回答，没有产物也没有代码，应当走默认卡片。" * 2) == SurfaceLevel.L4


def test_long_text_is_l5():
    assert route("x" * 900) == SurfaceLevel.L5


def test_artifacts_force_l5():
    assert route("anything", artifacts=["/ws/a.png"]) == SurfaceLevel.L5
    # artifacts win even over a short text
    assert route("ok", artifacts=["/ws/out.csv"]) == SurfaceLevel.L5


def test_codey_long_text_is_l5():
    body = "here is code:\n```python\nprint(1)\n```\n" + "x" * 200
    assert route(body) == SurfaceLevel.L5
    assert route("def foo(): pass  # " + "y" * 200) == SurfaceLevel.L5


def test_tiny_code_still_l1():
    # very short wins over code hint (it's just an acknowledgement)
    assert route("def f(): pass") == SurfaceLevel.L1
