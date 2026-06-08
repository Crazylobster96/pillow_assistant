"""apply_skill tool (T2): exposes local skills to the Agent through the tool loop."""

from __future__ import annotations

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult


class SkillTool:
    name = "apply_skill"
    permission = Permission.READONLY

    def __init__(self, skills) -> None:
        self._skills = {s.name: s for s in skills}
        listing = "\n".join(f"- {s.name}: {s.description}" for s in skills) or t("tool.skill.none")
        self.description = t("tool.skill.desc", listing=listing)
        self.parameters = {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": list(self._skills)}},
            "required": ["name"],
        }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = args.get("name", "")
        skill = self._skills.get(name)
        if skill is None:
            return ToolResult(ok=False, text=t("tool.skill.not_found", name=name))
        return ToolResult(ok=True, text=t("tool.skill.applied", name=skill.name,
                                          instructions=skill.instructions))
