"""Headless architecture checks for the floating UI composition shell."""

from pathlib import Path


def test_floating_widget_remains_a_small_composition_shell() -> None:
    source = Path(__file__).parents[1] / "pillow_assistant" / "ui" / "floating_widget.py"
    assert len(source.read_text(encoding="utf-8").splitlines()) <= 220
