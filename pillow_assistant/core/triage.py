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


def _system_prompt() -> str:
    return render_prompt("routing.project_triage.system")


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
        return TriageResult(action="chat", confidence=0.0, rationale="triage-failed")

    valid_ids = {str(p["id"]) for p in index}
    result = parse_triage(text, valid_ids)
    if result.action == "new" and not result.name:
        result.name = derive_name(prompt)
    return result
