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
    "- chat：简单的一问一答、闲聊、概念解释、单步小任务，无需建立项目。\n"
    "- 复杂工作（多步骤、要产出文件、需要持续推进）：若与某个已有项目同源，"
    "action=continue 并填它的 id；否则 action=new。"
)

_SYSTEM_EN = (
    "You are a task triager. Classify the user's request and output ONE JSON object, nothing else:\n"
    '{"action":"chat"|"continue"|"new","project_id":<project id when continue, else null>,'
    '"name":<a short English project name (<=4 words) when new, else null>,'
    '"confidence":0~1,"rationale":"brief reason"}\n'
    "Rules:\n"
    "- chat: simple Q&A, small talk, concept explanation, single-step micro tasks — no project needed.\n"
    "- Complex work (multi-step, produces files, ongoing): if it belongs to an existing project, "
    "action=continue with its id; otherwise action=new."
)


def _system_prompt() -> str:
    from pillow_assistant.core.i18n import LANG
    return _SYSTEM_ZH if LANG == "zh" else _SYSTEM_EN


async def triage(prompt: str, index: list[dict], *, cfg: dict, api_key: Optional[str],
                 current_id: Optional[str] = None) -> TriageResult:
    """Classify ``prompt`` (chat / continue / new) via the configured model."""
    listing = "\n".join(
        f'- id={p["id"]} 名称="{p.get("name","")}" 最近="{(p.get("last_prompt") or "")[:60]}"'
        for p in index[:20]
    ) or "（暂无项目）"
    hint = ""
    if current_id:
        hint = f"\n当前正在进行的项目 id={current_id}（若是它的延续工作，倾向 continue 该 id）。"
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": f"已有项目：\n{listing}{hint}\n\n新请求：{prompt}"},
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
