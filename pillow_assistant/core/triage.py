"""Three-way task triage (R1++):

* **chat** — a simple one-off question / chat / small single step → no project.
* **continue** — complex work that is same-origin as an existing project → resume it.
* **new** — complex work with no matching project → create a new project.

The LLM returns a small JSON object. ``parse_triage`` is pure and unit-testable;
the network call lives in ``triage`` and falls back to ``chat`` on failure so a
flaky classifier never spawns stray projects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from pillow_assistant.core import llm
from pillow_assistant.core.context_budget import join_context_and_prompt
from pillow_assistant.capabilities.prompt_registry import render_prompt

CONFIDENCE_THRESHOLD = 0.6


@dataclass
class TriageResult:
    action: str  # "chat" | "continue" | "new"
    project_id: Optional[str] = None
    name: Optional[str] = None
    confidence: float = 0.0
    rationale: str = ""
    prejudge: str = ""  # the keyword first pass shown to the model
    prejudge_ok: Optional[bool] = None  # whether the model kept it
    skill: str = ""  # the skill the run should start with ("" = none); loaded before the first step

    @property
    def is_chat(self) -> bool:
        return self.action == "chat"

    @property
    def low_confidence(self) -> bool:
        return self.confidence < CONFIDENCE_THRESHOLD


_APP_SETTING_PATTERNS = (
    r"(?:对话|展示|结果|应用|界面|窗口).{0,10}(?:透明度|透明|不透明|磨砂)",
    r"(?:透明度|磨砂).{0,10}(?:窗口|对话框|展示)",
    r"(?:模型配置|API\s*配置|接口地址|API\s*Key|默认对话模型)",
    r"(?:切换|设置|更改).{0,6}(?:界面)?语言",
    r"(?:最大|工具).{0,4}步数",
    r"(?:window|dialog|surface).{0,16}(?:transparen|opacity|frosted|acrylic)",
    r"(?:transparen|opacity|frosted|acrylic).{0,16}(?:window|dialog|surface)",
    r"(?:model|api)\s+(?:configuration|settings?)",
    r"(?:change|switch|set)\s+(?:the\s+)?(?:ui\s+)?language",
)


def is_app_setting_request(prompt: str) -> bool:
    """Return True for assistant-app settings that must never join a project."""
    text = re.sub(r"\s+", " ", prompt or "").strip()
    return bool(text) and any(re.search(pattern, text, re.IGNORECASE) for pattern in _APP_SETTING_PATTERNS)

_NEW_RE = re.compile(r"^(?:帮我|请)?(?:做|设计|画|新建|建|写|生成|创建|整理|搭)(?:一|个|款|套|份|张|块)")
_QUESTION_RE = re.compile(r"(?:吗|么|呢|多大|多少|怎么样?|如何|为什么|是什么|是不是|好不好|[?？])\s*$")
_REFERS_RE = re.compile(r"这个|这块|这张|这份|刚才|上次|继续|再|也|改成|改大|改小|调成|展示|导出|出[个张一]?")


def prejudge(prompt: str, index: list[dict], current: Optional[dict]) -> dict:
    """A quick keyword guess, handed to the model as a first pass it confirms or overrides."""
    text = re.sub(r"\s+", "", prompt or "")
    why = []
    named = next((p for p in index if p.get("name") and str(p["name"]) in text), None)
    if named is not None and current is not None and named["id"] != current["id"]:
        return {"guess": f"延续「{named['name']}」", "why": why + ["提到了这个项目的名字"],
                "action": "continue", "project_id": str(named["id"])}
    if _NEW_RE.match(text):
        return {"guess": "新建项目", "why": why + ["以「做/设计/新建一个」开头"], "action": "new"}
    if _QUESTION_RE.search(text) and len(text) <= 30 and not _REFERS_RE.search(text):
        return {"guess": "闲聊（一问一答）", "why": why + ["是个简短的问题，没有指代当前工作"], "action": "chat"}
    if current is not None and (len(text) <= 20 or _REFERS_RE.search(text)):
        return {"guess": f"延续当前项目「{current['name']}」", "why": why + ["没有说出新对象，或用了「这块/再/展示/导出」等指代"],
                "action": "continue", "project_id": str(current["id"])}
    return {"guess": "不确定", "why": why}


def derive_name(prompt: str, limit: int = 18) -> str:
    text = (prompt or "").strip().splitlines()[0] if prompt.strip() else ""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        from pillow_assistant.core.i18n import t
        return t("core.unnamed_task")
    return text if len(text) <= limit else text[:limit] + "…"


def parse_triage(text: str, valid_ids: set[str]) -> TriageResult:
    """Parse the model's JSON triage answer; tolerate surrounding prose."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return TriageResult(action="chat", confidence=0.0, rationale="no-json")
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return TriageResult(action="chat", confidence=0.0, rationale="bad-json")

    action = str(data.get("action", "chat")).lower()
    pid = data.get("project_id")
    pid = str(pid) if pid not in (None, "", "null") else None
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    rationale = str(data.get("rationale", ""))
    kept = data.get("prejudge_ok")
    kept = kept if isinstance(kept, bool) else None
    skill = str(data.get("skill") or "").strip()
    skill = "" if skill.lower() in ("null", "none", "-") else skill

    if action == "continue" and pid in valid_ids:
        return TriageResult(action="continue", project_id=pid, confidence=conf, rationale=rationale,
                            prejudge_ok=kept, skill=skill)
    if action == "new":
        return TriageResult(action="new", name=data.get("name"), confidence=conf, rationale=rationale,
                            prejudge_ok=kept, skill=skill)
    return TriageResult(action="chat", confidence=conf, rationale=rationale, prejudge_ok=kept, skill=skill)


def _system_prompt() -> str:
    return render_prompt("routing.project_triage.system")


_EXCEL_RE = re.compile(r"excel|xlsx|表格|工作簿|sheet", re.IGNORECASE)


def guess_skill(prompt: str, skills: list[str]) -> str:
    """The skill keywords point to, among those available ("" when none)."""
    if _EXCEL_RE.search(prompt or "") and "excel" in skills:
        return "excel"
    return ""


async def triage(prompt: str, index: list[dict], *, cfg: dict, api_key: Optional[str],
                 current_id: Optional[str] = None, current_context: str = "",
                 skills: Optional[list[tuple[str, str]]] = None) -> TriageResult:
    """Classify ``prompt`` (chat / continue / new) by the model's reading of its intent.

    ``current_context`` is the current project's last exchange, so a short
    follow-up ("把刚才的表再加一列") can be read against it."""
    if is_app_setting_request(prompt):
        return TriageResult(action="chat", confidence=1.0, rationale="app-setting")
    listing = "\n".join(
        f'- id={p["id"]} 名称="{p.get("name","")}" 最近="{(p.get("last_prompt") or "")[:60]}"'
        for p in index[:20]
    ) or "（暂无项目）"
    current = next((p for p in index if str(p["id"]) == str(current_id)), None) if current_id else None
    if current is not None:
        hint = f'\n\n当前项目：id={current["id"]} 名称="{current.get("name", "")}"'
        if current_context:
            hint += f"\n上一轮对话：\n{current_context}"
    else:
        hint = "\n\n当前没有打开的项目。"
    guess = prejudge(prompt, index, current)
    skill_names = [name for name, _ in (skills or [])]
    skill_guess = guess_skill(prompt, skill_names)
    if skill_guess:
        guess["why"] = list(guess["why"]) + [f"建议先加载技能 {skill_guess}"]
    first_pass = guess["guess"] + (f"（依据：{'；'.join(guess['why'])}）" if guess["why"] else "")
    hint += f"\n\n关键词初判：{first_pass}。请核对：准确就照此判断，不准确就按你的理解调整。"
    if skills:
        hint += "\n\n可用技能（选一个本次应先加载的，填进 skill；用不到就填 null）：\n" + "\n".join(
            f"- {name}：{desc}" for name, desc in skills)
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": join_context_and_prompt(
            f"已有项目：\n{listing}{hint}",
            render_prompt("routing.project_triage.request", prompt=prompt),
        )},
    ]
    try:
        text = await llm.complete(
            provider=cfg.get("provider", ""), model=cfg.get("model") or "",
            messages=messages, api_key=api_key, api_base=cfg.get("base_url"),
            extra=llm.parse_extra(cfg.get("extra")),
        )
    except Exception:
        # No classifier: the keyword first pass decides; with no guess, stay with the work at hand.
        action = guess.get("action")
        if action == "continue" and guess.get("project_id"):
            return TriageResult(action="continue", project_id=guess["project_id"], confidence=0.5,
                                rationale="triage-failed", prejudge=first_pass, skill=skill_guess)
        if action == "new":
            return TriageResult(action="new", name=derive_name(prompt), confidence=0.5, rationale="triage-failed",
                                prejudge=first_pass, skill=skill_guess)
        if action == "chat" or current is None:
            return TriageResult(action="chat", confidence=0.0, rationale="triage-failed", prejudge=first_pass, skill=skill_guess)
        return TriageResult(action="continue", project_id=str(current["id"]), confidence=0.5,
                            rationale="triage-failed", prejudge=first_pass, skill=skill_guess)

    valid_ids = {str(p["id"]) for p in index}
    result = parse_triage(text, valid_ids)
    result.prejudge = first_pass
    if result.skill and result.skill not in skill_names:
        result.skill = ""
    if result.action == "new" and not result.name:
        result.name = derive_name(prompt)
    return result
