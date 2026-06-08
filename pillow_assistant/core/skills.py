"""Local Skill library (T2).

A Skill is a small reusable capability: metadata (name, description, optional
tools) plus an instruction body the Agent follows. Skills live under
``~/.pillow/skills/`` as either ``<name>/SKILL.md`` or ``<name>.md`` with an
optional front-matter block::

    ---
    name: weekly-report
    description: 把目录里改动的文件整理成周报
    tools: run_python, file_write
    ---
    （给模型的操作指引正文……）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    tools: list = field(default_factory=list)
    source: str = ""


def parse_skill_md(text: str, fallback_name: str) -> Skill:
    name, description, tools, body = fallback_name, "", [], text
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
                tools = [t.strip() for t in v.strip("[]").split(",") if t.strip()]
    return Skill(name=name, description=description, instructions=body.strip(), tools=tools)


class SkillStore:
    def __init__(self, base: str | Path) -> None:
        self.base = Path(base)

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
        for d in sorted(p for p in self.base.iterdir() if p.is_dir()):
            f = d / "SKILL.md"
            if f.exists():
                out.append(self._load_file(f, d.name))
        for f in sorted(self.base.glob("*.md")):
            out.append(self._load_file(f, f.stem))
        return out
