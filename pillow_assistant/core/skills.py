"""Compatibility imports for the capability Skill loader."""

from pillow_assistant.capabilities.skill_loader import (
    MAX_NEST_DEPTH,
    Skill,
    SkillStore,
    parse_skill_md,
    resolve_skill,
    resolve_skill_tools,
)

__all__ = [
    "MAX_NEST_DEPTH",
    "Skill",
    "SkillStore",
    "parse_skill_md",
    "resolve_skill",
    "resolve_skill_tools",
]
