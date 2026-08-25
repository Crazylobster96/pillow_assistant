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

CONFIDENCE_THRESHOLD = 0.6


@dataclass
class TriageResult:
    action: str  # "chat" | "continue" | "new"
    project_id: Optional[str] = None
    name: Optional[str] = None
    confidence: float = 0.0
    rationale: str = ""

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

    if action == "continue" and pid in valid_ids:
        return TriageResult(action="continue", project_id=pid, confidence=conf, rationale=rationale)
    if action == "new":
        return TriageResult(action="new", name=data.get("name"), confidence=conf, rationale=rationale)
    return TriageResult(action="chat", confidence=conf, rationale=rationale)


_SYSTEM_ZH = (
    "你是任务分诊器。判断用户这次请求属于哪一类，只输出一个 JSON 对象，不要多余文字：\n"
    '{"action":"chat"|"continue"|"new","project_id":<continue时填项目id否则null>,'
    '"name":<new时给不超过12字的中文项目名否则null>,"confidence":0~1,"rationale":"简短理由"}\n'
    "判定规则：\n"
    "- chat：与已有项目无关的简单一问一答、闲聊、概念解释、单步小任务，无需建立项目。\n"
    "- continue：若本次请求明显是在延续/追问/修改某个已有项目的工作（即使措辞很短，"
    "如「继续」「把刚才的表再加一列」「上次那个方案改一下」），就 action=continue 并填该项目 id；"
    "宁可归到最相关的项目，也不要轻易当 chat 丢掉上下文。\n"
    "- new：复杂工作（多步骤、要产出文件、需持续推进）且与任何已有项目都不同源时，action=new。"
)

_SYSTEM_EN = (
    "You are a task triager. Classify the user's request and output ONE JSON object, nothing else:\n"
    '{"action":"chat"|"continue"|"new","project_id":<project id when continue, else null>,'
    '"name":<a short English project name (<=4 words) when new, else null>,'
    '"confidence":0~1,"rationale":"brief reason"}\n'
    "Rules:\n"
    "- chat: simple Q&A / small talk / concept explanation / single-step micro tasks UNRELATED to any "
    "existing project.\n"
    "- continue: if the request clearly continues / follows up on / edits an existing project's work "
    "(even if phrased briefly, e.g. \"continue\", \"add a column to that table\", \"tweak last plan\"), "
    "set action=continue with that project id; prefer attaching to the most relevant project over "
    "dropping context as chat.\n"
    "- new: complex work (multi-step, produces files, ongoing) that matches no existing project → action=new."
)


def _system_prompt() -> str:
    from pillow_assistant.core.i18n import LANG
    return _SYSTEM_ZH if LANG == "zh" else _SYSTEM_EN


async def triage(prompt: str, index: list[dict], *, cfg: dict, api_key: Optional[str],
                 current_id: Optional[str] = None) -> TriageResult:
    """Classify ``prompt`` (chat / continue / new) via the configured model."""
    if is_app_setting_request(prompt):
        return TriageResult(action="chat", confidence=1.0, rationale="app-setting")
    listing = "\n".join(
        f'- id={p["id"]} 名称="{p.get("name","")}" 最近="{(p.get("last_prompt") or "")[:60]}"'
        for p in index[:20]
    ) or "（暂无项目）"
    hint = ""
    if current_id:
        hint = f"\n当前正在进行的项目 id={current_id}（若是它的延续工作，倾向 continue 该 id）。"
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": join_context_and_prompt(
            f"已有项目：\n{listing}{hint}",
            (
                f"新请求：{prompt}\n\n"
                "请根据支持材料完成项目分诊，并严格按系统消息要求只返回 JSON。"
            ),
        )},
    ]
    try:
        text = await llm.complete(
            provider=cfg.get("provider", ""), model=cfg.get("model") or "",
            messages=messages, api_key=api_key, api_base=cfg.get("base_url"),
            extra=llm.parse_extra(cfg.get("extra")),
        )
    except Exception:
        return TriageResult(action="chat", confidence=0.0, rationale="triage-failed")

    valid_ids = {str(p["id"]) for p in index}
    result = parse_triage(text, valid_ids)
    if result.action == "new" and not result.name:
        result.name = derive_name(prompt)
    return result
