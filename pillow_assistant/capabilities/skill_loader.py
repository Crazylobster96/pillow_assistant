"""Local Skill library (T2) with declarative nesting.

A Skill is a small reusable capability: metadata (name, description, optional
tools) plus an instruction body the Agent follows. Skills live under
``~/.pillow/skills/`` as either ``<name>/SKILL.md`` or ``<name>.md`` with an
optional front-matter block::

    ---
    name: weekly-report
    description: 把目录里改动的文件整理成周报
    tools: run_python, file_write
    extends: report-base          # 继承：父技能正文排在前
    includes: [collect-changes]   # 组合：被引技能正文排在后
    ---
    （给模型的操作指引正文……）

Nesting is resolved at load time: ``extends`` skills are inlined *before* this
skill's body (inheritance), ``includes`` skills *after* it (composition). The
expansion is recursive with cycle detection and a max depth, so a skill the
Agent applies already contains its whole nested instruction set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pillow_assistant.core.i18n import t

MAX_NEST_DEPTH = 6


@dataclass
class Skill:
    name: str
    description: str
    instructions: str                       # this skill's own body (raw)
    tools: list = field(default_factory=list)
    extends: list = field(default_factory=list)   # parent skill names (inlined before)
    includes: list = field(default_factory=list)  # sub-skill names (inlined after)
    source: str = ""
    resolved_instructions: str = ""         # body with extends/includes expanded
    children: list = field(default_factory=list)  # all sub-skills actually merged
    resolved_tools: list = field(default_factory=list)
    source_kind: str = "unknown"


def _parse_list(v: str) -> list:
    return [x.strip() for x in v.strip("[]").split(",") if x.strip()]


def parse_skill_md(text: str, fallback_name: str) -> Skill:
    name, description, tools, body = fallback_name, "", [], text
    extends, includes = [], []
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        front, body = m.group(1), m.group(2)
        for line in front.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "name" and v:
                name = v
            elif k == "description":
                description = v
            elif k == "tools":
                tools = _parse_list(v)
            elif k == "extends":
                extends = _parse_list(v)
            elif k == "includes":
                includes = _parse_list(v)
    return Skill(name=name, description=description, instructions=body.strip(),
                 tools=tools, extends=extends, includes=includes)


def resolve_skill(skill: Skill, by_name: dict, _stack: tuple = ()) -> tuple[str, list]:
    """Expand a skill's ``extends``/``includes`` into one instruction text.

    Returns ``(text, children)`` where children are the sub-skill names merged
    (transitively). Cycles and missing refs become inline notes instead of
    errors; recursion is capped at ``MAX_NEST_DEPTH``.
    """
    if skill.name in _stack:
        return t("skill.nest_cycle", name=skill.name), []
    if len(_stack) >= MAX_NEST_DEPTH:
        return skill.instructions + "\n" + t("skill.nest_depth"), []
    stack = _stack + (skill.name,)
    parts: list[str] = []
    children: list[str] = []

    def merge(dep: str, header_key: str) -> None:
        child = by_name.get(dep)
        if child is None:
            parts.append(t("skill.nest_missing", name=dep))
            return
        sub_text, sub_children = resolve_skill(child, by_name, stack)
        parts.append(t(header_key, name=dep) + "\n" + sub_text)
        children.append(dep)
        children.extend(sub_children)

    for dep in skill.extends:           # inheritance: parents first
        merge(dep, "skill.nest_extends")
    if skill.instructions:
        parts.append(skill.instructions)
    for dep in skill.includes:          # composition: sub-skills after
        merge(dep, "skill.nest_includes")

    # De-dup children while preserving order.
    seen, uniq = set(), []
    for c in children:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return "\n\n".join(p for p in parts if p), uniq


def resolve_skill_tools(skill: Skill, by_name: dict, _stack: tuple = ()) -> list:
    """Collect declared tools across extends/includes without granting permissions."""
    if skill.name in _stack or len(_stack) >= MAX_NEST_DEPTH:
        return list(skill.tools)
    stack = _stack + (skill.name,)
    tools = list(skill.tools)
    for dependency in list(skill.extends) + list(skill.includes):
        child = by_name.get(dependency)
        if child is not None:
            tools.extend(resolve_skill_tools(child, by_name, stack))
    seen: set[str] = set()
    return [name for name in tools if name and not (name in seen or seen.add(name))]

class SkillStore:
    def __init__(self, base: Any) -> None:
        self.base = Path(base) if isinstance(base, (str, Path)) else base

    def _load_file(self, f: Path, fallback: str) -> Skill:
        try:
            text = f.read_text("utf-8")
        except OSError:
            text = ""
        skill = parse_skill_md(text, fallback)
        skill.source = str(f)
        return skill

    def load(self) -> list[Skill]:
        out: list[Skill] = []
        if not self.base.is_dir():
            return out
        children = sorted(self.base.iterdir(), key=lambda item: item.name)
        for directory in (item for item in children if item.is_dir()):
            file = directory.joinpath("SKILL.md")
            if file.is_file():
                out.append(self._load_file(file, directory.name))
        for file in (item for item in children if item.is_file() and item.name.lower().endswith(".md")):
            out.append(self._load_file(file, Path(file.name).stem))

        # Resolve nesting once all skills are loaded.
        by_name = {s.name: s for s in out}
        for s in out:
            text, children = resolve_skill(s, by_name)
            s.resolved_instructions = text
            s.children = children
            s.resolved_tools = resolve_skill_tools(s, by_name)
        return out
