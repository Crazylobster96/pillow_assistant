"""Tests: model roles persistence, role-aware routing, self-config tools."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from pillow_assistant.core.model_roles import assign, load_roles, save_roles
from pillow_assistant.core.model_router import select_model
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.config_tools import (
    AssignModelRoleTool,
    ConfigureModelTool,
    ListModelsTool,
    SetLanguageTool,
    SetSurfaceTransparencyTool,
)

CFGS = [
    {"display_name": "A-llm", "model_type": "llm"},
    {"display_name": "B-vlm", "model_type": "vlm"},
    {"display_name": "C-vlm", "model_type": "vlm"},
    {"display_name": "D-llm", "model_type": "llm"},
]


class FakeVault:
    def __init__(self):
        self.secrets = {"Old": "sk-old"}

    def get_secret(self, name):
        return self.secrets.get(name)


class FakeStorage:
    def __init__(self):
        self.rows = [{"display_name": "Old", "provider": "OpenAI", "model_type": "llm",
                      "model": "gpt-4o", "base_url": None, "extra": ""}]
        self.last_replace = None

    def list_model_configs(self):
        return [dict(r) for r in self.rows]

    def replace_model_configs(self, configs, vault):
        self.last_replace = [dict(c) for c in configs]
        self.rows = [{k: v for k, v in c.items() if k != "api_key"} for c in configs]


def test_roles_roundtrip(tmp_path):
    p = tmp_path / "roles.json"
    assert load_roles(p) == {}
    assign("chat", "Qwen", p)
    assign("asr", {"backend": "whisper", "model": "medium"}, p)
    r = load_roles(p)
    assert r["chat"] == "Qwen" and r["asr"]["model"] == "medium"


def test_routing_priorities():
    assert select_model(CFGS, "A-llm") == "A-llm"  # explicit wins
    assert select_model(CFGS, None, roles={"chat": "D-llm"}) == "D-llm"
    assert select_model(CFGS, "A-llm", want_vision=True, roles={"vision": "C-vlm"}) == "C-vlm"
    assert select_model(CFGS, "A-llm", want_vision=True) == "B-vlm"
    assert select_model(CFGS, "B-vlm", want_vision=True, roles={"vision": "X"}) == "B-vlm"
    assert select_model(CFGS, None, roles={"chat": "GONE"}) == "A-llm"


def test_configure_model_add_and_update(tmp_path):
    st, va = FakeStorage(), FakeVault()
    ctx = ToolContext(workspace=Path(tmp_path), storage=st, vault=va)
    tool = ConfigureModelTool()

    r = asyncio.run(tool({"display_name": "Local", "provider": "Ollama",
                          "model": "qwen2.5", "base_url": "http://localhost:11434"}, ctx))
    assert r.ok
    assert any(c["display_name"] == "Local" for c in st.rows)
    # existing key survived the replace
    assert next(c for c in st.last_replace if c["display_name"] == "Old")["api_key"] == "sk-old"

    r = asyncio.run(tool({"display_name": "Old", "model": "gpt-4o-mini",
                          "model_type": "vlm"}, ctx))
    assert r.ok
    assert next(c for c in st.last_replace if c["display_name"] == "Old")["api_key"] == "sk-old"
    assert next(c for c in st.rows if c["display_name"] == "Old")["model"] == "gpt-4o-mini"


def test_configure_model_validation(tmp_path):
    ctx = ToolContext(workspace=Path(tmp_path), storage=FakeStorage())
    assert not asyncio.run(ConfigureModelTool()({"display_name": "", "model": ""}, ctx)).ok
    ctx2 = ToolContext(workspace=Path(tmp_path))  # no storage
    assert not asyncio.run(ConfigureModelTool()({"display_name": "X", "model": "m"}, ctx2)).ok


def test_assign_role(tmp_path, monkeypatch):
    import pillow_assistant.core.model_roles as mr
    monkeypatch.setattr(mr, "roles_path", lambda: tmp_path / "roles.json")
    st = FakeStorage()
    ctx = ToolContext(workspace=Path(tmp_path), storage=st)
    tool = AssignModelRoleTool()

    assert not asyncio.run(tool({"role": "boss", "model": "Old"}, ctx)).ok  # bad role
    assert not asyncio.run(tool({"role": "chat", "model": "Nope"}, ctx)).ok  # unknown model
    assert asyncio.run(tool({"role": "chat", "model": "Old"}, ctx)).ok
    assert mr.load_roles()["chat"] == "Old"
    assert not asyncio.run(tool({"role": "asr", "model": "siri"}, ctx)).ok  # bad backend
    assert asyncio.run(tool({"role": "asr", "model": "whisper", "whisper_size": "medium"}, ctx)).ok
    assert mr.load_roles()["asr"] == {"backend": "whisper", "model": "medium"}


def test_list_models(tmp_path):
    ctx = ToolContext(workspace=Path(tmp_path), storage=FakeStorage())
    r = asyncio.run(ListModelsTool()({}, ctx))
    assert r.ok and "Old" in r.text


def test_set_language_roundtrip():
    import pillow_assistant.core.i18n as i18n
    old = i18n.LANG
    try:
        tool = SetLanguageTool()
        ctx = ToolContext(workspace=Path("."))
        assert not asyncio.run(tool({"lang": "fr"}, ctx)).ok
        assert asyncio.run(tool({"lang": "en"}, ctx)).ok
        assert i18n.LANG == "en" and i18n.t("menu.quit") == "Quit"
        assert asyncio.run(tool({"lang": "zh"}, ctx)).ok
        assert i18n.t("menu.quit") == "退出"
    finally:
        i18n.set_language(old)


def test_surface_transparency_exact_endpoints(tmp_path, monkeypatch):
    import pillow_assistant.core.settings as settings

    state = {"surface_glass_opacity": 68}
    notified = []
    monkeypatch.setattr(settings, "load_settings", lambda: dict(state))
    monkeypatch.setattr(settings, "set_setting", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setitem(
        sys.modules,
        "pillow_assistant.ui.acrylic",
        SimpleNamespace(notify_glass_opacity=notified.append),
    )

    tool = SetSurfaceTransparencyTool()
    ctx = ToolContext(workspace=Path(tmp_path))
    cases = (
        ({"mode": "set", "transparency": 100}, 0, "transparency is now 100%"),
        ({"mode": "set", "transparency": 0}, 100, "transparency is now 0%"),
        ({"mode": "set", "opacity": 100}, 100, "background opacity 100%"),
        ({"mode": "set", "opacity": 0}, 0, "background opacity 0%"),
    )
    for args, expected_opacity, expected_text in cases:
        result = asyncio.run(tool(args, ctx))
        assert result.ok
        assert state["surface_glass_opacity"] == expected_opacity
        assert notified[-1] == expected_opacity
        assert expected_text in result.text

    result = asyncio.run(
        tool({"mode": "set", "transparency": 50, "opacity": 50}, ctx)
    )
    assert not result.ok
    assert "exactly one" in result.text
