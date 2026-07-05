from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from pillow_assistant.core import llm
from storage.conversation import ConversationMemoryStore


RECENT_TOPIC_LIMIT = 8
RECENT_TURN_LIMIT = 6
RELEVANT_TURN_LIMIT = 4
MEMORY_SIGNAL_LIMIT = 6


@dataclass
class ConversationRoute:
    kind: str
    topic_id: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    keywords: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""

    @property
    def uses_topic(self) -> bool:
        return self.kind in {"existing_topic", "new_topic"}


@dataclass
class ConversationContext:
    route: ConversationRoute
    topic: Optional[dict] = None
    recent_turns: list[dict] = field(default_factory=list)
    relevant_turns: list[dict] = field(default_factory=list)
    memory_signals: list[dict] = field(default_factory=list)
    rendered_context: str = ""


def is_greeting(prompt: str) -> bool:
    text = (prompt or "").strip().lower()
    if not text:
        return False
    greetings = {
        "hi", "hello", "hey", "你好", "您好", "早", "早上好", "晚上好", "在吗", "嗨",
    }
    return text in greetings or (len(text) <= 8 and any(g in text for g in ("你好", "早上好", "hello")))


def looks_one_off_qa(prompt: str) -> bool:
    text = (prompt or "").strip()
    if not text:
        return False
    if any(x in text for x in ("继续", "刚才", "上次", "那个", "这个方案", "接着")):
        return False
    question_mark = "?" in text or "？" in text
    simple_starters = ("什么是", "如何", "怎么", "解释", "介绍一下", "what is", "how to")
    return question_mark or text.lower().startswith(simple_starters)


def _short(text: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]{3,}", (text or "").lower())
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    seen: list[str] = []
    for item in words + cjk:
        if item not in seen:
            seen.append(item)
        if len(seen) >= limit:
            break
    return seen


def _parse_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def _fmt_time(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


class ConversationRouter:
    def __init__(self, store: ConversationMemoryStore) -> None:
        self.store = store

    async def route(self, prompt: str, *, cfg: dict, api_key: Optional[str]) -> ConversationRoute:
        if is_greeting(prompt):
            return ConversationRoute(kind="greeting", confidence=1.0, reason="greeting-rule")

        topics = self.store.list_recent_topics(RECENT_TOPIC_LIMIT)
        if not topics:
            if looks_one_off_qa(prompt):
                return ConversationRoute(kind="one_off_qa", confidence=0.7, reason="no-topics-one-off")
            return ConversationRoute(
                kind="new_topic", title=_short(prompt, 24), summary=_short(prompt, 120),
                keywords=_keywords(prompt), confidence=0.7, reason="no-topics",
            )

        listing = "\n".join(
            f'- id={t["id"]} title="{t.get("title","")}" updated={_fmt_time(t.get("last_message_at") or t.get("updated_at"))} '
            f'summary="{_short(t.get("summary",""), 120)}" keywords={",".join(t.get("keywords") or [])}'
            for t in topics
        )
        messages = [
            {"role": "system", "content": (
                "You are a conversation topic router. Output ONE JSON object only. "
                "Kinds: greeting, one_off_qa, existing_topic, new_topic. "
                "One-off factual questions should be one_off_qa unless they clearly continue an existing topic. "
                "Prefer continuing the most recent related topic; create a new topic only when the subject clearly changes."
            )},
            {"role": "user", "content": (
                f"Recent topics:\n{listing}\n\nUser message:\n{prompt}\n\n"
                '{"kind":"greeting|one_off_qa|existing_topic|new_topic","topic_id":null,'
                '"title":null,"summary":null,"keywords":[],"confidence":0.0,"reason":"brief"}'
            )},
        ]
        try:
            raw = await llm.complete(
                provider=cfg.get("provider", ""), model=cfg.get("model") or "",
                messages=messages, api_key=api_key, api_base=cfg.get("base_url"),
                extra=llm.parse_extra(cfg.get("extra")),
            )
            data = _parse_json(raw) or {}
        except Exception:
            data = {}

        valid_ids = {t["id"] for t in topics}
        kind = str(data.get("kind") or "").strip()
        topic_id = data.get("topic_id")
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if kind == "existing_topic" and topic_id in valid_ids and confidence >= 0.45:
            return ConversationRoute(
                kind="existing_topic", topic_id=str(topic_id), confidence=confidence,
                reason=str(data.get("reason", "")),
            )
        if kind == "new_topic" and confidence >= 0.45:
            return ConversationRoute(
                kind="new_topic", title=str(data.get("title") or _short(prompt, 24)),
                summary=str(data.get("summary") or _short(prompt, 120)),
                keywords=[str(x) for x in (data.get("keywords") or _keywords(prompt))][:8],
                confidence=confidence, reason=str(data.get("reason", "")),
            )
        if kind == "greeting":
            return ConversationRoute(kind="greeting", confidence=confidence, reason=str(data.get("reason", "")))
        if looks_one_off_qa(prompt):
            return ConversationRoute(kind="one_off_qa", confidence=max(confidence, 0.5), reason="fallback-one-off")
        recent = topics[0]
        return ConversationRoute(kind="existing_topic", topic_id=recent["id"], confidence=0.35, reason="fallback-recent")


class ConversationContextBuilder:
    def __init__(self, store: ConversationMemoryStore) -> None:
        self.store = store

    def build(self, route: ConversationRoute, prompt: str) -> ConversationContext:
        ctx = ConversationContext(route=route)
        if route.kind == "new_topic":
            ctx.topic = self.store.create_topic(
                route.title or _short(prompt, 24),
                route.summary or _short(prompt, 120),
                route.keywords or _keywords(prompt),
            )
            route.topic_id = ctx.topic["id"]
        elif route.kind == "existing_topic" and route.topic_id:
            ctx.topic = self.store.get_topic(route.topic_id)

        if ctx.topic:
            ctx.recent_turns = self.store.recent_turns(ctx.topic["id"], RECENT_TURN_LIMIT)
            ctx.relevant_turns = self.store.search_relevant_turns(
                prompt, RELEVANT_TURN_LIMIT, exclude_topic_id=ctx.topic["id"]
            )
            ctx.memory_signals = self._relevant_signals(prompt)
            ctx.rendered_context = render_context(ctx)
        return ctx

    def _relevant_signals(self, prompt: str) -> list[dict]:
        signals = self.store.list_user_memory_signals(status="active", limit=30)
        if not signals:
            signals = self.store.list_user_memory_signals(status="candidate", limit=30)
        prompt_tokens = set(_keywords(prompt, 20))
        scored = []
        for signal in signals:
            text = signal.get("content", "")
            overlap = len(prompt_tokens & set(_keywords(text, 20)))
            score = overlap + float(signal.get("confidence") or 0.0)
            if score > 0:
                scored.append((score, signal))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:MEMORY_SIGNAL_LIMIT]]


def render_context(ctx: ConversationContext) -> str:
    if not ctx.topic:
        return ""
    lines = [
        "[Top-level conversation topic]",
        f"Title: {ctx.topic.get('title', '')}",
        f"Summary: {ctx.topic.get('summary', '')}",
        f"Last updated: {_fmt_time(ctx.topic.get('last_message_at') or ctx.topic.get('updated_at'))}",
        "",
        "[Recent turns in the same topic]",
    ]
    if ctx.recent_turns:
        for turn in ctx.recent_turns:
            user = turn.get("user_summary") or _short(turn.get("user_text", ""), 120)
            assistant = turn.get("assistant_summary") or _short(turn.get("assistant_text", ""), 120)
            lines.append(f"- {_fmt_time(turn.get('timestamp'))} User: {user}")
            if assistant:
                lines.append(f"  Assistant: {assistant}")
    else:
        lines.append("- (none yet)")

    lines.extend(["", "[Most relevant historical turns]"])
    if ctx.relevant_turns:
        for turn in ctx.relevant_turns:
            topic = turn.get("topic_title") or turn.get("topic_id") or "-"
            user = turn.get("user_summary") or _short(turn.get("user_text", ""), 120)
            assistant = turn.get("assistant_summary") or _short(turn.get("assistant_text", ""), 120)
            lines.append(f"- {_fmt_time(turn.get('timestamp'))} Source topic: {topic}; User: {user}")
            if assistant:
                lines.append(f"  Assistant: {assistant}")
    else:
        lines.append("- (none)")

    if ctx.memory_signals:
        lines.extend(["", "[User memory beta]"])
        for signal in ctx.memory_signals:
            lines.append(f"- {signal.get('type')}: {signal.get('content')} (confidence={signal.get('confidence')})")
    return "\n".join(lines)


class ConversationWriteback:
    def __init__(self, store: ConversationMemoryStore) -> None:
        self.store = store

    async def record(self, ctx: ConversationContext, prompt: str, answer: str, *, cfg: dict, api_key: Optional[str]) -> None:
        if not ctx.route.uses_topic or not ctx.route.topic_id:
            return
        summary = await self._summarize(prompt, answer, cfg=cfg, api_key=api_key)
        turn = self.store.append_turn(
            ctx.route.topic_id, prompt, answer,
            user_summary=summary.get("user_summary") or _short(prompt, 120),
            assistant_summary=summary.get("assistant_summary") or _short(answer, 120),
            keywords=summary.get("keywords") or _keywords(prompt + " " + answer),
            importance=float(summary.get("importance") or 0.0),
        )
        topic_summary = summary.get("topic_summary")
        if topic_summary:
            self.store.update_topic(
                ctx.route.topic_id, summary=topic_summary,
                keywords=summary.get("topic_keywords") or summary.get("keywords") or [],
            )
        for signal in await self._extract_signals(prompt, answer, turn.get("id"), cfg=cfg, api_key=api_key):
            self.store.add_user_memory_signal(
                signal.get("type") or "preference",
                signal.get("content") or "",
                confidence=float(signal.get("confidence") or 0.0),
                source_turn_id=turn.get("id"),
                needs_confirmation=bool(signal.get("needs_confirmation")),
                status=signal.get("status") or "candidate",
            )

    async def _summarize(self, prompt: str, answer: str, *, cfg: dict, api_key: Optional[str]) -> dict:
        messages = [
            {"role": "system", "content": "Summarize this conversation turn as JSON only."},
            {"role": "user", "content": (
                f"User:\n{prompt}\n\nAssistant:\n{answer}\n\n"
                '{"user_summary":"","assistant_summary":"","topic_summary":"","keywords":[],"topic_keywords":[],"importance":0.0}'
            )},
        ]
        try:
            raw = await llm.complete(
                provider=cfg.get("provider", ""), model=cfg.get("model") or "",
                messages=messages, api_key=api_key, api_base=cfg.get("base_url"),
                extra=llm.parse_extra(cfg.get("extra")),
            )
            data = _parse_json(raw) or {}
        except Exception:
            data = {}
        return data

    async def _extract_signals(self, prompt: str, answer: str, turn_id: Optional[str], *, cfg: dict, api_key: Optional[str]) -> list[dict]:
        text = prompt + "\n" + answer
        obvious = any(x in prompt for x in ("以后", "总是", "经常", "每天", "提醒我", "常用", "偏好"))
        if not obvious:
            return []
        messages = [
            {"role": "system", "content": (
                "Extract reusable user memory signals as JSON only. "
                "Return [] if none. Types: preference, frequent_need, frequent_tool, schedule_candidate. "
                "Schedule/reminder candidates must needs_confirmation=true and status=candidate."
            )},
            {"role": "user", "content": text},
        ]
        try:
            raw = await llm.complete(
                provider=cfg.get("provider", ""), model=cfg.get("model") or "",
                messages=messages, api_key=api_key, api_base=cfg.get("base_url"),
                extra=llm.parse_extra(cfg.get("extra")),
            )
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []


class ConversationMemoryService:
    def __init__(self, store: ConversationMemoryStore) -> None:
        self.store = store
        self.router = ConversationRouter(store)
        self.builder = ConversationContextBuilder(store)
        self.writeback = ConversationWriteback(store)

    async def prepare_chat_context(self, prompt: str, *, cfg: dict, api_key: Optional[str]) -> ConversationContext:
        route = await self.router.route(prompt, cfg=cfg, api_key=api_key)
        return self.builder.build(route, prompt)

    async def record_chat_result(self, ctx: ConversationContext, prompt: str, answer: str, *, cfg: dict, api_key: Optional[str]) -> None:
        await self.writeback.record(ctx, prompt, answer, cfg=cfg, api_key=api_key)
