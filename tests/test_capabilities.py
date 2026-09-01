from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pillow_assistant.capabilities.prompt_registry import PromptRegistryError, get_prompt_registry
from pillow_assistant.capabilities.skill_registry import CapabilitySkillRegistry
from pillow_assistant.capabilities.tool_manifest import ToolManifestError, ToolManifestRegistry
from pillow_assistant.core.skills import SkillStore
from pillow_assistant.core.tools.base import Permission, ToolContext
from pillow_assistant.core.tools.builtin import build_default_registry
from pillow_assistant.core.tools.builtin.python_tool import PythonTool
from pillow_assistant.core.tools.builtin.skill_tool import SkillTool


def _skill(root: Path, folder: str, body: str) -> None:
    target = root / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(body, "utf-8")


def test_prompt_catalog_is_file_backed_versioned_and_strict():
    registry = get_prompt_registry()
    assert len(registry.ids()) >= 23
    assert "瞌睡送枕头" in registry.render("shared.main_agent", language="zh")
    assert "local agent" in registry.render("shared.main_agent", language="en")
    rendered = registry.render(
        "routing.project_triage.request", variables={"prompt": "继续修复"}, language="zh"
    )
    assert "继续修复" in rendered
    literal_marker = registry.render(
        "routing.project_triage.request", variables={"prompt": "保留 {{literal}}"}, language="zh"
    )
    assert "保留 {{literal}}" in literal_marker
    metadata = registry.metadata("shared.main_agent", language="zh")
    assert metadata["version"] == "1.0.0"
    assert metadata["source"].endswith("main-agent.zh.md")
    assert len(metadata["sha256"]) == 64
    with pytest.raises(PromptRegistryError, match="missing variables"):
        registry.render("routing.project_triage.request", language="zh")
    with pytest.raises(PromptRegistryError, match="unknown variables"):
        registry.render("shared.main_agent", variables={"unused": "x"})


def test_tool_manifests_bind_metadata_and_filter_modes():
    manifests = ToolManifestRegistry(language="zh")
    registry = build_default_registry()
    assert len(manifests.names()) == 19
    assert len(registry.names()) == 18
    assert manifests.get("apply_skill").permission == "readonly"
    assert "request_project_memory" not in registry.names("chat")
    assert "request_project_memory" in registry.names("project")
    assert len(registry.schemas("chat")) == 17
    assert len(registry.schemas("project")) == 18
    assert PythonTool.description
    assert PythonTool.parameters["required"] == ["code"]
    snapshot = registry.snapshot("project")
    assert all(item["version"] == "1.0.0" for item in snapshot)
    assert all("manifest.json" in item["source"] for item in snapshot)


def test_mode_filter_is_enforced_at_dispatch():
    registry = build_default_registry()
    ctx = ToolContext(workspace=Path("."), project_id=None)
    result = asyncio.run(registry.dispatch("request_project_memory", {"query": "x"}, ctx))
    assert not result.ok
    assert "not available in chat mode" in result.text


def test_manifest_cannot_elevate_or_downgrade_implementation_permission(tmp_path: Path):
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({
        "name": "demo",
        "version": "1.0.0",
        "description": {"zh": "demo", "en": "demo"},
        "parameters": {
            "zh": {"type": "object", "properties": {}},
            "en": {"type": "object", "properties": {}},
        },
        "permission": "system",
        "modes": ["chat", "project"],
    }), "utf-8")
    registry = ToolManifestRegistry(tmp_path)
    tool = SimpleNamespace(name="demo", permission=Permission.READONLY)
    with pytest.raises(ToolManifestError, match="Permission mismatch"):
        registry.bind(tool)


def test_layered_skills_override_and_resolve_cross_source_dependencies(tmp_path: Path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for root in (builtin, user, project):
        root.mkdir()
    _skill(builtin, "base", "---\nname: base\ntools: run_python\n---\nBASE")
    _skill(user, "middle", "---\nname: middle\nextends: base\n---\nMIDDLE")
    _skill(project, "workflow", "---\nname: workflow\nincludes: middle\ntools: file_write\n---\nWORKFLOW")
    _skill(builtin, "same", "---\nname: same\n---\nBUILTIN")
    _skill(user, "same", "---\nname: same\n---\nUSER")
    _skill(project, "same", "---\nname: same\n---\nPROJECT")

    registry = CapabilitySkillRegistry(
        built_in_root=builtin, user_root=user, project_root=project
    )
    skills = {skill.name: skill for skill in registry.load()}
    assert skills["same"].instructions == "PROJECT"
    assert skills["same"].source_kind == "project"
    workflow = skills["workflow"]
    assert all(text in workflow.resolved_instructions for text in ("BASE", "MIDDLE", "WORKFLOW"))
    assert set(workflow.resolved_tools) == {"run_python", "file_write"}


def test_apply_skill_rejects_missing_declared_tools(tmp_path: Path):
    _skill(tmp_path, "needs-write", "---\nname: needs-write\ntools: file_write\n---\nWRITE")
    skill = SkillStore(tmp_path).load()[0]
    blocked = SkillTool([skill], available_tools={"run_python"})
    assert blocked.capability_version == "1.0.0"
    assert blocked.parameters["properties"]["name"]["enum"] == ["needs-write"]
    result = asyncio.run(blocked({"name": "needs-write"}, ToolContext(workspace=tmp_path)))
    assert not result.ok
    assert "file_write" in result.text
    allowed = SkillTool([skill], available_tools={"run_python", "file_write"})
    result = asyncio.run(allowed({"name": "needs-write"}, ToolContext(workspace=tmp_path)))
    assert result.ok
    assert "WRITE" in result.text
