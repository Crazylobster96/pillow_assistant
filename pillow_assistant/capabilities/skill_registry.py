"""Discover built-in, user, and project Skill files with explicit precedence."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Iterable, Optional

from pillow_assistant.capabilities.skill_loader import Skill, SkillStore, resolve_skill, resolve_skill_tools


class CapabilitySkillRegistry:
    """Load skills in increasing precedence: built-in, user, then project."""

    def __init__(
        self,
        *,
        built_in_root: Optional[str | Path] = None,
        user_root: Optional[str | Path] = None,
        project_root: Optional[str | Path] = None,
    ) -> None:
        default_builtin = resources.files("pillow_assistant.capabilities").joinpath("skills", "builtin")
        self.built_in_root = Path(built_in_root) if built_in_root is not None else default_builtin
        self.user_root = Path(user_root) if user_root is not None else Path.home() / ".pillow" / "skills"
        self.project_root = Path(project_root) if project_root is not None else None
        self._skills: list[Skill] = []

    def _sources(self) -> list[tuple[str, object]]:
        result: list[tuple[str, object]] = [("builtin", self.built_in_root), ("user", self.user_root)]
        if self.project_root is not None:
            result.append(("project", self.project_root))
        return result

    def load(self) -> list[Skill]:
        selected: dict[str, Skill] = {}
        for source_kind, root in self._sources():
            try:
                skills = SkillStore(root).load()
            except (OSError, TypeError):
                skills = []
            for skill in skills:
                skill.source_kind = source_kind
                selected[skill.name] = skill
        self._skills = list(selected.values())
        by_name = {skill.name: skill for skill in self._skills}
        for skill in self._skills:
            skill.resolved_instructions, skill.children = resolve_skill(skill, by_name)
            skill.resolved_tools = resolve_skill_tools(skill, by_name)
        return list(self._skills)

    def validate_tools(self, skill: Skill, available_tools: Iterable[str]) -> list[str]:
        available = set(available_tools)
        required = getattr(skill, "resolved_tools", None) or skill.tools
        return sorted({str(name) for name in required if str(name) not in available})

    def snapshot(self) -> list[dict]:
        if not self._skills:
            self.load()
        return [
            {
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "source_kind": getattr(skill, "source_kind", "unknown"),
                "tools": list(getattr(skill, "resolved_tools", None) or skill.tools),
                "children": list(skill.children),
            }
            for skill in self._skills
        ]
