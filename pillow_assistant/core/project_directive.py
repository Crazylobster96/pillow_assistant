"""Let the user say which project a message belongs to, and what it is called.

Recognised forms (anywhere in the message)::

    项目：齿轮箱            / project: gearbox
    归入「齿轮箱」项目       / put this in the gearbox project
    新建项目「齿轮箱 v2」    / new project "gearbox v2"
    项目名为 齿轮箱          / name the project gearbox

``parse_project_directive`` is pure; ``resolve_directive`` matches the name
against the project index so triage can skip its model call. Renaming an
existing project (改名 / rename) is left to the rename_project tool.

``parse_inherit_source`` finds a project to build on ("基于「齿轮箱」项目",
"用齿轮箱项目的模型", "based on the gearbox project"): its outputs are copied
into the project this message lands in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_Q_OPEN = "「『“\"'《"
_Q_CLOSE = "」』”\"'》"
_QUOTED = rf"[{_Q_OPEN}]([^{_Q_CLOSE}\n]{{1,40}})[{_Q_CLOSE}]"
# An unquoted name runs to the next punctuation / line end (spaces allowed).
_BARE = r"([^\s，。,.;；！!？?、:：\n][^，。,.;；！!？?、\n]{0,39})"
_BARE_WORD = r"([^\s，。,.;；！!？?、:：\n「『“\"'《]{1,30})"
_LEAD = r"(?:^|(?<=[\s，。,.;；！!？?、]))"

_NEW_ZH = r"(?:新建|创建|新开|(?<![打展公])开|另起|另开)(?:一个)?(?:新的?)?项目"
_NEW_EN = r"(?<![a-z])(?:new|(?:create|start|open)\s+a\s+new|create\s+a)\s+project"

_RENAME_RE = re.compile(r"改名|重命名|更名|rename", re.IGNORECASE)

# (mode, pattern) — first match wins. "new" always creates a project;
# "assign" joins a same-named project or creates it when none exists;
# "join" (looser phrasing such as "put this in the X project") only joins an
# existing project and otherwise leaves the decision to triage.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (mode, re.compile(pattern, re.IGNORECASE)) for mode, pattern in (
        # A bare name needs a separator (，/：/叫/名为): "新建项目做个机械臂" is not a name.
        ("new", rf"{_NEW_ZH}\s*(?:(?:[，,：:]|叫做?|名为|命名为)\s*)?{_QUOTED}"),
        ("new", rf"{_NEW_ZH}\s*(?:[，,：:]|叫做?|名为|命名为)\s*{_BARE_WORD}"),
        ("new", rf"{_NEW_EN}\s*(?:(?:called|named|:)\s*)?{_QUOTED}"),
        ("new", rf"{_NEW_EN}\s*(?:called|named|:)\s*{_BARE_WORD}"),
        ("assign", rf"{_LEAD}项目(?:名称?|名字)?\s*[：:]\s*(?:{_QUOTED}|{_BARE})"),
        ("assign", rf"项目(?:名称?|名字)?\s*(?:命名为|取名为?|定名为|名为|叫做?)\s*(?:{_QUOTED}|{_BARE_WORD})"),
        ("assign", rf"(?:命名|取名)(?:这个|该|本)?项目(?:为|叫)\s*(?:{_QUOTED}|{_BARE_WORD})"),
        ("join", rf"(?:归入|归到|归档到|放到|放进|放入|加入|记到|存到|并入|属于)\s*(?:{_QUOTED}|{_BARE_WORD})\s*(?:这个)?项目"),
        ("assign", rf"{_LEAD}project\s*[：:]\s*(?:{_QUOTED}|{_BARE})"),
        ("join", rf"(?<![a-z])(?:in|into|to|under|for)\s+(?:the\s+)?project\s+(?:{_QUOTED}|{_BARE_WORD})"),
        ("join", rf"(?<![a-z])(?:in|into|to|under|for)\s+(?:the\s+)?(?:{_QUOTED}|{_BARE_WORD})\s+project(?![a-z])"),
        ("assign", rf"(?<![a-z])(?:name|call)\s+(?:the|this)\s+project\s+(?:{_QUOTED}|{_BARE_WORD})"),
    )
)

# "新建项目" / "new project" with no name: still a new project, named by triage.
_NEW_UNNAMED = re.compile(
    r"(?:新建|创建|(?<![打展公])开|另起|另开)(?:一个)?新的?项目(?!的)|(?:新建|另起|另开)(?:一个)?项目(?!的)"
    r"|(?<![a-z])(?:start|open|create)\s+a\s+new\s+project(?![a-z])",
    re.IGNORECASE,
)
# A bare Chinese name stops before a space that leads into more Chinese text
# ("项目：齿轮箱 帮我做外壳" -> 齿轮箱), but keeps "齿轮箱 v2" / "gear box".
_TRAILING_CJK = re.compile(r"\s+[\u4e00-\u9fff].*$")

# Words that look like a name slot but are not one ("put it in this project").
_NOT_NAMES = {"这个", "该", "本", "当前", "那个", "新", "一个", "this", "that", "the", "current", "a", "new", "same"}


_INHERIT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    rf"(?:基于|继承|沿用|借用|复用)\s*(?:{_QUOTED}|{_BARE_WORD})\s*项目",
    rf"在\s*(?:{_QUOTED}|{_BARE_WORD})\s*项目的?(?:成果|模型|基础|结果)(?:的基础)?上",
    rf"(?:用|拿|使用|引用|导入)\s*(?:{_QUOTED}|{_BARE_WORD})\s*项目的?(?:成果|模型|文件|零件|图纸|产出|结果|STEP|STL|GLB)",
    rf"(?<![a-z])(?:based\s+on|build(?:ing)?\s+on|inherit(?:ing)?(?:\s+from)?|reus(?:e|ing)|using)\s+(?:the\s+)?"
    rf"(?:{_QUOTED}|{_BARE_WORD})\s+project(?![a-z])",
    rf"(?<![a-z])(?:based\s+on|build(?:ing)?\s+on|inherit(?:ing)?(?:\s+from)?|reus(?:e|ing))\s+(?:the\s+)?project\s+"
    rf"(?:{_QUOTED}|{_BARE_WORD})",
))


def parse_inherit_source(prompt: str) -> Optional[str]:
    """Name of the project whose outputs this message wants to build on."""
    for pattern in _INHERIT_PATTERNS:
        match = pattern.search(prompt or "")
        if match:
            name = (match.group(1) or match.group(2) or "").strip()
            if name and name.lower() not in _NOT_NAMES:
                return name
    return None


@dataclass
class ProjectDirective:
    mode: str  # "assign" | "join" | "new"
    name: str  # "" only for an unnamed "new"


def parse_project_directive(prompt: str) -> Optional[ProjectDirective]:
    text = (prompt or "").strip()
    if not text or _RENAME_RE.search(text):
        return None
    for mode, pattern in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        quoted = match.group(1)  # every pattern puts the quoted form first
        bare = match.group(2) if pattern.groups > 1 else None
        if pattern.groups == 1 and not pattern.pattern.endswith(_QUOTED):
            quoted, bare = None, quoted
        name = (quoted or _TRAILING_CJK.sub("", bare or "")).strip()
        if name and (quoted or name.lower() not in _NOT_NAMES):
            return ProjectDirective(mode=mode, name=name)
    if _NEW_UNNAMED.search(text):
        return ProjectDirective(mode="new", name="")
    return None


def match_project(name: str, index: list[dict]) -> Optional[dict]:
    """An index entry named ``name``: exact (case-insensitive) or a unique substring match."""
    low = name.strip().lower()
    if not low:
        return None
    exact = [p for p in index if str(p.get("name", "")).strip().lower() == low]
    if exact:
        return exact[0]
    partial = [p for p in index if low in str(p.get("name", "")).lower()]
    return partial[0] if len(partial) == 1 else None


def resolve_directive(directive: ProjectDirective, index: list[dict]) -> Optional[tuple[str, Optional[str], str]]:
    """(action, project_id, name) for a parsed directive, or None when it does
    not decide anything ("join" with no such project). name may be "" for an
    unnamed "new"."""
    if directive.mode in ("assign", "join"):
        found = match_project(directive.name, index)
        if found is not None:
            return "continue", str(found["id"]), str(found.get("name") or directive.name)
        if directive.mode == "join":
            return None
    return "new", None, directive.name
