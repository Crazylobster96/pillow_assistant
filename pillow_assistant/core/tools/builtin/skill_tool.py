"""apply_skill tool (T2): exposes local skills to the Agent through the tool loop."""

from __future__ import annotations

from pillow_assistant.capabilities.prompt_registry import render_prompt
from pillow_assistant.capabilities.tool_manifest import get_tool_manifest_registry
from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult


def skill_tools(skill) -> set:
    """Tools a skill works with (held back from the model until the skill is applied)."""
    return set(getattr(skill, "resolved_tools", None) or skill.tools or [])


class SkillTool:
    name = "apply_skill"
    permission = Permission.READONLY

    def __init__(self, skills, available_tools=None) -> None:
        get_tool_manifest_registry().bind(self)
        self._skills = {s.name: s for s in skills}
        # This tool itself is registered after the list is taken; skills such as cam
        # apply others (dfm, dxf), so it must count as available.
        available = set(available_tools or []) | {self.name}
        self._missing_tools = {
            s.name: sorted(set(getattr(s, "resolved_tools", None) or s.tools) - available)
            for s in skills
        } if available_tools is not None else {}
        listing = "\n".join(f"- {s.name}: {s.description}" for s in skills) or t("tool.skill.none")
        self.description = render_prompt("skill.apply.description", listing=listing)
        name_schema = self.parameters.setdefault("properties", {}).setdefault("name", {"type": "string"})
        name_schema["enum"] = list(self._skills)

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = args.get("name", "")
        skill = self._skills.get(name)
        missing = self._missing_tools.get(name) or []
        if skill is None:
            return ToolResult(ok=False, text=t("tool.skill.not_found", name=name))
        if missing:
            return ToolResult(ok=False, text=f"Skill {name} requires unavailable tools: {', '.join(missing)}")
        # Nested skills are already inlined into resolved_instructions; surface
        # which sub-skills were merged so the model knows the scope.
        instructions = getattr(skill, "resolved_instructions", "") or skill.instructions
        children = getattr(skill, "children", None)
        if children:
            instructions = t("tool.skill.merged", names="、".join(children)) + "\n\n" + instructions
        active = getattr(ctx, "active_tools", None)
        if isinstance(active, set):
            active.update(skill_tools(skill))
        return ToolResult(ok=True, text=t("tool.skill.applied", name=skill.name,
                                          instructions=instructions))
