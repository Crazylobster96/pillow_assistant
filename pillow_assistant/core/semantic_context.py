"""Model-assisted, loss-minimizing compression for very large contexts.

The coordinator starts semantic compression in the background before the hard
boundary, switches to a verified structured capsule at the configured ratio,
and leaves deterministic compaction as the final safety net.  A candidate is
always rebuilt from the raw source supplied for that request; an older capsule
is never recursively summarized into a newer one.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pillow_assistant.core import context_budget


CAPSULE_OPEN = "<pillow_semantic_context_capsule>"
CAPSULE_CLOSE = "</pillow_semantic_context_capsule>"
CAPSULE_KEYS = (
    "requirements",
    "decisions",
    "facts",
    "artifacts",
    "tool_state",
    "open_questions",
    "exact_fragments",
    "conflicts",
)
ACTIVE_TOOL_SOURCE_THRESHOLD = 2_048

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

CompletionCall = Callable[[list[dict[str, Any]], int], Awaitable[str]]


@dataclass(frozen=True)
class SemanticCompressionSettings:
    enabled: bool = True
    min_context_tokens: int = 65_536
    min_source_tokens: int = 2_048
    background_start_ratio: float = 0.60
    prepare_ratio: float = 0.75
    switch_ratio: float = 0.85
    chunk_tokens: int = 24_000
    chunk_summary_tokens: int = 2_048
    summary_max_tokens: int = 6_000
    verify: bool = True
    switch_wait_seconds: float = 90.0
    max_concurrency: int = 3

    @classmethod
    def from_context_config(
        cls, context_config: Optional[dict[str, Any]]
    ) -> "SemanticCompressionSettings":
        config = context_config or {}
        raw = config.get("semantic_compression", {})
        if raw is False:
            return cls(enabled=False)
        if raw is True or not isinstance(raw, dict):
            raw = {}

        background = _ratio(raw.get("background_start_ratio"), 0.60)
        prepare = max(background + 0.02, _ratio(raw.get("prepare_ratio"), 0.75))
        switch = max(prepare + 0.02, _ratio(raw.get("switch_ratio"), 0.85))
        return cls(
            enabled=_boolean(raw.get("enabled"), True),
            min_context_tokens=_positive_int(raw.get("min_context_tokens"), 65_536),
            min_source_tokens=_positive_int(raw.get("min_source_tokens"), 2_048),
            background_start_ratio=min(background, 0.93),
            prepare_ratio=min(prepare, 0.96),
            switch_ratio=min(switch, 0.98),
            chunk_tokens=_positive_int(raw.get("chunk_tokens"), 24_000, minimum=2_048),
            chunk_summary_tokens=_positive_int(
                raw.get("chunk_summary_tokens"), 2_048, minimum=512
            ),
            summary_max_tokens=_positive_int(raw.get("summary_max_tokens"), 6_000, minimum=768),
            verify=_boolean(raw.get("verify"), True),
            switch_wait_seconds=_positive_float(raw.get("switch_wait_seconds"), 90.0),
            max_concurrency=_positive_int(raw.get("max_concurrency"), 3, minimum=1, maximum=8),
        )


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    text: str


@dataclass
class SemanticSource:
    records: list[SourceRecord]
    head_messages: list[dict[str, Any]]
    active_messages: list[dict[str, Any]]
    digest: str
    source_tokens: int
    active_request: str = ""

    @property
    def source_ids(self) -> set[str]:
        return {record.source_id for record in self.records}

    @property
    def source_text(self) -> str:
        return "\n\n".join(record.text for record in self.records)

    def apply(self, capsule: dict[str, Any], *, verified: bool) -> list[dict[str, Any]]:
        capsule_text = render_capsule(capsule, verified=verified)
        return (
            copy.deepcopy(self.head_messages)
            + [{"role": "system", "content": capsule_text}]
            + copy.deepcopy(self.active_messages)
        )


@dataclass
class SemanticCandidate:
    capsule: dict[str, Any]
    verified: bool
    source_digest: str
    source_tokens: int
    source_count: int
    chunk_count: int


class SemanticContextCoordinator:
    """Coordinates non-blocking preparation and verified capsule switching."""

    def __init__(self, *, cache_size: int = 16) -> None:
        self.cache_size = max(1, cache_size)
        self._cache: OrderedDict[str, SemanticCandidate] = OrderedDict()
        self._tasks: dict[str, asyncio.Task[Optional[SemanticCandidate]]] = {}

    async def manage(
        self,
        *,
        manager: context_budget.ContextBudgetManager,
        provider: str,
        api_base: Optional[str],
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        model_info: Optional[dict[str, Any]],
        context_config: Optional[dict[str, Any]],
        completion_extra: Optional[dict[str, Any]],
        compressor_key: str,
        completion_call: CompletionCall,
        emergency: bool = False,
    ) -> context_budget.ContextManagementResult:
        if emergency:
            result = manager.prepare(
                provider=provider,
                api_base=api_base,
                model=model,
                messages=messages,
                tools=tools,
                model_info=model_info,
                context_config=context_config,
                completion_extra=completion_extra,
                emergency=True,
            )
            result.semantic_status = "emergency-deterministic"
            return result

        settings = SemanticCompressionSettings.from_context_config(context_config)
        key = context_budget.model_profile_key(provider, api_base, model)
        limits = manager.resolve_limits(
            model_info=model_info,
            context_config=context_config,
            completion_extra=completion_extra,
        )
        before = manager.estimate(messages, tools, key)
        ratio = before.adjusted_tokens / max(1, limits.hard_input_tokens)
        effective_window = limits.hard_input_tokens + limits.output_reserve_tokens

        if not settings.enabled or effective_window < settings.min_context_tokens:
            result = manager.prepare(
                provider=provider,
                api_base=api_base,
                model=model,
                messages=messages,
                tools=tools,
                model_info=model_info,
                context_config=context_config,
                completion_extra=completion_extra,
            )
            result.semantic_status = "disabled" if not settings.enabled else "small-context"
            return result

        source = build_semantic_source(messages)
        if source is None or source.source_tokens < settings.min_source_tokens:
            result = manager.prepare(
                provider=provider,
                api_base=api_base,
                model=model,
                messages=messages,
                tools=tools,
                model_info=model_info,
                context_config=context_config,
                completion_extra=completion_extra,
            )
            result.semantic_status = "no-compressible-source"
            return result

        cache_key = _cache_key(compressor_key, source.digest, settings)
        candidate = self._cache_get(cache_key)
        task: Optional[asyncio.Task[Optional[SemanticCandidate]]] = None
        if ratio >= settings.background_start_ratio and candidate is None:
            task = self._task_for(
                cache_key,
                source=source,
                settings=settings,
                completion_call=completion_call,
            )

        switch_ratio = min(
            settings.switch_ratio,
            limits.soft_input_tokens / max(1, limits.hard_input_tokens),
        )
        if ratio < switch_ratio:
            if before.adjusted_tokens >= limits.soft_input_tokens:
                result = manager.prepare(
                    provider=provider,
                    api_base=api_base,
                    model=model,
                    messages=messages,
                    tools=tools,
                    model_info=model_info,
                    context_config=context_config,
                    completion_extra=completion_extra,
                )
            else:
                result = context_budget.ContextManagementResult(
                    messages=messages,
                    estimate_before=before,
                    estimate_after=before,
                    limits=limits,
                    model_key=key,
                )
            if candidate is not None:
                result.semantic_status = "candidate-ready"
            elif task is not None:
                result.semantic_status = (
                    "candidate-preparing"
                    if ratio >= settings.prepare_ratio
                    else "background-preparing"
                )
            else:
                result.semantic_status = "below-background-threshold"
            result.semantic_source_tokens = source.source_tokens
            return result

        if candidate is None:
            task = task or self._task_for(
                cache_key,
                source=source,
                settings=settings,
                completion_call=completion_call,
            )
            try:
                candidate = await asyncio.wait_for(
                    asyncio.shield(task), timeout=settings.switch_wait_seconds
                )
            except asyncio.TimeoutError:
                candidate = None
            except Exception:
                candidate = None

        if candidate is None:
            result = manager.prepare(
                provider=provider,
                api_base=api_base,
                model=model,
                messages=messages,
                tools=tools,
                model_info=model_info,
                context_config=context_config,
                completion_extra=completion_extra,
            )
            result.semantic_status = "fallback-deterministic"
            result.semantic_source_tokens = source.source_tokens
            return result

        semantic_messages = source.apply(candidate.capsule, verified=candidate.verified)
        result = manager.prepare(
            provider=provider,
            api_base=api_base,
            model=model,
            messages=semantic_messages,
            tools=tools,
            model_info=model_info,
            context_config=context_config,
            completion_extra=completion_extra,
        )
        result.estimate_before = before
        result.compacted = True
        result.semantic_compressed = True
        result.semantic_verified = candidate.verified
        result.semantic_status = "verified-capsule" if candidate.verified else "capsule"
        result.semantic_source_tokens = candidate.source_tokens
        result.semantic_source_count = candidate.source_count
        result.semantic_chunk_count = candidate.chunk_count
        return result

    def _cache_get(self, key: str) -> Optional[SemanticCandidate]:
        candidate = self._cache.get(key)
        if candidate is not None:
            self._cache.move_to_end(key)
        return candidate

    def _task_for(
        self,
        key: str,
        *,
        source: SemanticSource,
        settings: SemanticCompressionSettings,
        completion_call: CompletionCall,
    ) -> asyncio.Task[Optional[SemanticCandidate]]:
        existing = self._tasks.get(key)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._compress_source(source, settings=settings, completion_call=completion_call),
            name=f"pillow-semantic-context-{source.digest[:10]}",
        )
        self._tasks[key] = task

        def finished(done: asyncio.Task[Optional[SemanticCandidate]]) -> None:
            self._tasks.pop(key, None)
            try:
                candidate = done.result()
            except (Exception, asyncio.CancelledError):
                return
            if candidate is None:
                return
            self._cache[key] = candidate
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

        task.add_done_callback(finished)
        return task

    async def _compress_source(
        self,
        source: SemanticSource,
        *,
        settings: SemanticCompressionSettings,
        completion_call: CompletionCall,
    ) -> Optional[SemanticCandidate]:
        chunks = chunk_source(source.records, settings.chunk_tokens)
        if not chunks:
            return None
        semaphore = asyncio.Semaphore(settings.max_concurrency)

        async def compress_one(chunk: str) -> Optional[dict[str, Any]]:
            async with semaphore:
                capsule = await _compress_chunk(
                    chunk,
                    active_request=source.active_request,
                    completion_call=completion_call,
                    max_tokens=settings.chunk_summary_tokens,
                )
                if capsule is None:
                    return None
                if settings.verify:
                    capsule = await _verify_chunk(
                        chunk,
                        capsule,
                        completion_call=completion_call,
                        max_tokens=min(settings.chunk_summary_tokens, 2_048),
                    )
                return capsule

        capsules = await asyncio.gather(*(compress_one(chunk) for chunk in chunks))
        if any(capsule is None for capsule in capsules):
            return None
        normalized = [capsule for capsule in capsules if capsule is not None]
        capsule = await _reduce_capsules(
            normalized,
            completion_call=completion_call,
            chunk_tokens=settings.chunk_tokens,
            max_tokens=settings.summary_max_tokens,
            verify=settings.verify,
        )
        if capsule is None:
            return None
        capsule = _validate_capsule(capsule, source)
        if settings.verify and not _has_provenance(capsule, source.source_ids):
            return None
        return SemanticCandidate(
            capsule=capsule,
            verified=settings.verify,
            source_digest=source.digest,
            source_tokens=source.source_tokens,
            source_count=len(source.records),
            chunk_count=len(chunks),
        )


def build_semantic_source(messages: list[dict[str, Any]]) -> Optional[SemanticSource]:
    """Split archived/supporting content from protected active messages."""
    last_user = _last_role_index(messages, "user")
    if last_user is None:
        return None

    head: list[dict[str, Any]] = []
    records: list[SourceRecord] = []
    for index, message in enumerate(messages[:last_user]):
        if message.get("role") in {"system", "developer"}:
            head.append(copy.deepcopy(message))
            continue
        records.append(
            SourceRecord(
                source_id=f"message:{index}",
                text=_message_source_text(message),
            )
        )

    active = copy.deepcopy(messages[last_user:])
    supporting, replaced = _remove_supporting_context(active[0].get("content"))
    if supporting:
        records.append(
            SourceRecord(
                source_id=f"message:{last_user}:supporting-context",
                text=supporting,
            )
        )
        active[0]["content"] = replaced

    active_request = _current_request_text(active[0].get("content"))
    for offset, message in enumerate(messages[last_user + 1 :], 1):
        if message.get("role") != "tool":
            continue
        source_text = _message_source_text(message)
        if context_budget.estimate_text_tokens(source_text) < ACTIVE_TOOL_SOURCE_THRESHOLD:
            continue
        source_id = f"message:{last_user + offset}:tool-result"
        records.append(SourceRecord(source_id=source_id, text=source_text))
        active[offset]["content"] = (
            "[Large tool result moved to the semantic context capsule; "
            f"source_id={source_id}. The raw result remains in the caller or Agent session.]"
        )

    records = [record for record in records if record.text.strip()]
    if not records:
        return None
    canonical = "\n".join(f"{record.source_id}\0{record.text}" for record in records)
    canonical += f"\nACTIVE_REQUEST\0{active_request}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return SemanticSource(
        records=records,
        head_messages=head,
        active_messages=active,
        digest=digest,
        source_tokens=sum(context_budget.estimate_text_tokens(record.text) for record in records),
        active_request=active_request,
    )


def chunk_source(records: list[SourceRecord], max_tokens: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for record in records:
        fragments = _split_record(record, max_tokens)
        for fragment in fragments:
            tokens = context_budget.estimate_text_tokens(fragment)
            if current and current_tokens + tokens > max_tokens:
                chunks.append("\n\n".join(current))
                current = []
                current_tokens = 0
            current.append(fragment)
            current_tokens += tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def render_capsule(capsule: dict[str, Any], *, verified: bool) -> str:
    status = "verified" if verified else "unverified"
    payload = json.dumps(capsule, ensure_ascii=False, indent=2, default=str)
    return (
        f"{CAPSULE_OPEN}\n"
        f"status: {status}\n"
        "This capsule replaces archived source text. Preserve its requirements, decisions, "
        "exact fragments, unresolved questions, and source IDs. Source IDs refer to raw messages "
        "retained by the running Agent session or to existing conversation, project, and file "
        "stores; request the original when an available retrieval tool is needed.\n"
        f"{payload}\n"
        f"{CAPSULE_CLOSE}"
    )


def resolve_compression_profile(storage: Any, vault: Any, main_cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Resolve a configured compression model without persisting its secret in JSON extra."""
    extra = _json_object(main_cfg.get("extra"))
    pillow_context = extra.get("pillow_context", {}) if isinstance(extra, dict) else {}
    semantic = (
        pillow_context.get("semantic_compression", {})
        if isinstance(pillow_context, dict)
        else {}
    )
    requested = semantic.get("model_ref") if isinstance(semantic, dict) else None
    if not requested:
        try:
            from pillow_assistant.core.model_roles import load_roles

            requested = load_roles().get("compression")
        except Exception:
            requested = None
    if not requested or storage is None or not hasattr(storage, "get_model_config"):
        return None
    cfg = storage.get_model_config(str(requested))
    if not cfg:
        return None
    name = cfg.get("display_name", "")
    return {
        "provider": cfg.get("provider", ""),
        "model": cfg.get("model") or "",
        "api_base": cfg.get("base_url"),
        "api_key": vault.get_secret(name) if vault is not None and name else None,
        "extra": _json_object(cfg.get("extra")),
        "display_name": name,
    }


async def _compress_chunk(
    source: str,
    *,
    active_request: str,
    completion_call: CompletionCall,
    max_tokens: int,
) -> Optional[dict[str, Any]]:
    messages = [
        {
            "role": "system",
            "content": (
                "CONTEXT_COMPRESSOR\n"
                "You are a loss-minimizing context compressor. Extract only; never infer, "
                "invent, resolve conflicts, or mark work complete without explicit evidence. "
                "Preserve user requirements, decisions with reasons, facts, file paths, versions, "
                "numbers, commands, code/API identifiers, tool state, errors, open questions, and "
                "conflicts. Every list item must contain source_ids copied from the supplied source "
                "labels. Put text that must remain verbatim in exact_fragments. Return one JSON "
                "object only, in the source language."
            ),
        },
        {
            "role": "user",
            "content": (
                "CURRENT TASK (use only to rank relevance; it is preserved separately and is not "
                "evidence for source-attributed facts):\n"
                f"{_query_hint(active_request)}\n\n"
                f"SOURCE MATERIAL:\n{source}\n\n"
                "Return this schema:\n"
                '{"current_goal":"","requirements":[],"decisions":[],"facts":[],"artifacts":[], '
                '"tool_state":[],"open_questions":[],"exact_fragments":[],"conflicts":[],"source_ids":[]}'
            ),
        },
    ]
    try:
        return _normalize_capsule(_parse_json(await completion_call(messages, max_tokens)))
    except Exception:
        return None


async def _verify_chunk(
    source: str,
    capsule: dict[str, Any],
    *,
    completion_call: CompletionCall,
    max_tokens: int,
) -> Optional[dict[str, Any]]:
    messages = [
        {
            "role": "system",
            "content": (
                "CONTEXT_VERIFIER\n"
                "Compare the capsule against the source. Reject invented facts, changed numbers, "
                "lost requirements/decisions/open work, invalid source IDs, and altered exact "
                "fragments. Return JSON only. If invalid, provide a complete corrected_capsule."
            ),
        },
        {
            "role": "user",
            "content": (
                f"SOURCE:\n{source}\n\nCAPSULE:\n"
                f"{json.dumps(capsule, ensure_ascii=False)}\n\n"
                '{"valid":true,"missing_critical":[],"distortions":[],"corrected_capsule":null}'
            ),
        },
    ]
    try:
        result = _parse_json(await completion_call(messages, max_tokens))
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    if result.get("valid") is True:
        return capsule
    corrected = _normalize_capsule(result.get("corrected_capsule"))
    return corrected if corrected else None


async def _reduce_capsules(
    capsules: list[dict[str, Any]],
    *,
    completion_call: CompletionCall,
    chunk_tokens: int,
    max_tokens: int,
    verify: bool,
) -> Optional[dict[str, Any]]:
    if not capsules:
        return None
    current = capsules
    for _level in range(6):
        if len(current) == 1:
            return _normalize_capsule(current[0])
        groups = _capsule_groups(current, chunk_tokens)
        reduced: list[dict[str, Any]] = []
        for group in groups:
            if len(group) == 1:
                reduced.append(group[0])
                continue
            messages = [
                {
                    "role": "system",
                    "content": (
                        "CONTEXT_REDUCER\n"
                        "Merge verified context capsules without inventing or dropping requirements, "
                        "decisions, exact fragments, open questions, conflicts, or source IDs. Deduplicate "
                        "only genuinely identical items. Return the capsule schema as one JSON object."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(group, ensure_ascii=False, default=str),
                },
            ]
            try:
                merged = _normalize_capsule(await _json_completion(completion_call, messages, max_tokens))
            except Exception:
                return None
            if not merged:
                return None
            if verify:
                merged = await _verify_chunk(
                    json.dumps(group, ensure_ascii=False, default=str),
                    merged,
                    completion_call=completion_call,
                    max_tokens=min(max_tokens, 2_048),
                )
                if merged is None:
                    return None
            reduced.append(merged)
        if len(reduced) >= len(current):
            return _local_merge(reduced)
        current = reduced
    return _local_merge(current)


async def _json_completion(
    completion_call: CompletionCall,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> Any:
    return _parse_json(await completion_call(messages, max_tokens))


def _capsule_groups(capsules: list[dict[str, Any]], max_tokens: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    tokens = 0
    for capsule in capsules:
        value = json.dumps(capsule, ensure_ascii=False, default=str)
        estimate = context_budget.estimate_text_tokens(value)
        if current and tokens + estimate > max_tokens:
            groups.append(current)
            current = []
            tokens = 0
        current.append(capsule)
        tokens += estimate
    if current:
        groups.append(current)
    return groups


def _local_merge(capsules: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"current_goal": "", **{key: [] for key in CAPSULE_KEYS}, "source_ids": []}
    goals: list[str] = []
    seen: dict[str, set[str]] = {key: set() for key in CAPSULE_KEYS}
    sources: set[str] = set()
    for capsule in capsules:
        goal = str(capsule.get("current_goal") or "").strip()
        if goal and goal not in goals:
            goals.append(goal)
        for source_id in capsule.get("source_ids", []):
            if isinstance(source_id, str):
                sources.add(source_id)
        for key in CAPSULE_KEYS:
            for item in capsule.get(key, []):
                signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if signature not in seen[key]:
                    seen[key].add(signature)
                    merged[key].append(item)
    merged["current_goal"] = " / ".join(goals)
    merged["source_ids"] = sorted(sources)
    return merged


def _validate_capsule(capsule: dict[str, Any], source: SemanticSource) -> dict[str, Any]:
    valid_ids = source.source_ids
    result = _normalize_capsule(capsule) or {}
    result["source_ids"] = sorted(
        source_id for source_id in result.get("source_ids", []) if source_id in valid_ids
    )
    for key in CAPSULE_KEYS:
        cleaned: list[dict[str, Any]] = []
        for raw_item in result.get(key, []):
            item = dict(raw_item) if isinstance(raw_item, dict) else {"text": str(raw_item)}
            ids = [source_id for source_id in item.get("source_ids", []) if source_id in valid_ids]
            item["source_ids"] = ids
            if key == "exact_fragments":
                exact = str(item.get("text") or item.get("content") or "")
                if exact and exact not in source.source_text:
                    continue
            cleaned.append(item)
        result[key] = cleaned
    return result


def _has_provenance(capsule: dict[str, Any], valid_ids: set[str]) -> bool:
    top_ids = capsule.get("source_ids", [])
    if not isinstance(top_ids, list) or any(source_id not in valid_ids for source_id in top_ids):
        return False
    cited = {source_id for source_id in top_ids if source_id in valid_ids}
    for key in CAPSULE_KEYS:
        for item in capsule.get(key, []):
            ids = item.get("source_ids", []) if isinstance(item, dict) else []
            if not ids or any(source_id not in valid_ids for source_id in ids):
                return False
            cited.update(ids)
    # A goal without any source reference is still an unsupported claim.  At
    # least one valid citation is required even when all detail lists are empty.
    return bool(cited)


def _normalize_capsule(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {
        "current_goal": str(value.get("current_goal") or "").strip(),
        "source_ids": _string_list(value.get("source_ids")),
    }
    for key in CAPSULE_KEYS:
        raw_items = value.get(key, [])
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if isinstance(raw, str):
                item: dict[str, Any] = {"text": raw, "source_ids": []}
            elif isinstance(raw, dict):
                item = {
                    str(name): content
                    for name, content in raw.items()
                    if isinstance(content, (str, int, float, bool, list)) or content is None
                }
                item["source_ids"] = _string_list(raw.get("source_ids"))
            else:
                continue
            if str(item.get("text") or item.get("content") or item.get("decision") or "").strip() or len(item) > 1:
                items.append(item)
        result[key] = items
    return result


def _parse_json(text: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    value = str(text or "").strip()
    value = _JSON_FENCE_RE.sub("", value).strip()
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(value[start : end + 1])
            except ValueError:
                return None
    return None


def _message_source_text(message: dict[str, Any]) -> str:
    safe = copy.deepcopy(message)
    content = safe.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            image = block.get("image_url")
            if isinstance(image, dict) and str(image.get("url") or "").startswith("data:"):
                image["url"] = "[archived image data omitted from text compressor]"
    return json.dumps(safe, ensure_ascii=False, default=str)


def _remove_supporting_context(content: Any) -> tuple[str, Any]:
    if isinstance(content, str):
        return _remove_marked_text(content)
    if not isinstance(content, list):
        return "", content
    extracted: list[str] = []
    updated = copy.deepcopy(content)
    for block in updated:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        source, replaced = _remove_marked_text(block["text"])
        if source:
            extracted.append(source)
            block["text"] = replaced
    return "\n\n".join(extracted), updated


def _remove_marked_text(text: str) -> tuple[str, str]:
    start = text.find(context_budget.CONTEXT_OPEN)
    end = text.find(context_budget.CONTEXT_CLOSE)
    if start < 0 or end <= start:
        return "", text
    body_start = start + len(context_budget.CONTEXT_OPEN)
    source = text[body_start:end].strip("\n")
    replacement = (
        f"{context_budget.CONTEXT_OPEN}\n"
        "[Supporting context moved to the semantic context capsule with source provenance.]\n"
        f"{context_budget.CONTEXT_CLOSE}"
    )
    return source, text[:start] + replacement + text[end + len(context_budget.CONTEXT_CLOSE) :]


def _current_request_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            _current_request_text(block.get("text"))
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ).strip()
    text = str(content or "")
    start = text.find(context_budget.REQUEST_OPEN)
    end = text.find(context_budget.REQUEST_CLOSE)
    if start >= 0 and end > start:
        body_start = start + len(context_budget.REQUEST_OPEN)
        return text[body_start:end].strip("\n")
    return text.strip()


def _query_hint(text: str, limit: int = 8_000) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    half = max(1, (limit - 80) // 2)
    return value[:half] + "\n...[current task middle omitted for relevance hint]...\n" + value[-half:]


def _split_record(record: SourceRecord, max_tokens: int) -> list[str]:
    prefix = f"[SOURCE {record.source_id}]\n"
    suffix = f"\n[/SOURCE {record.source_id}]"
    wrapped = prefix + record.text + suffix
    if context_budget.estimate_text_tokens(wrapped) <= max_tokens:
        return [wrapped]
    estimate = max(1, context_budget.estimate_text_tokens(record.text))
    chars = max(512, int(len(record.text) * max_tokens / estimate * 0.82))
    pieces = [record.text[index : index + chars] for index in range(0, len(record.text), chars)]
    width = len(str(len(pieces)))
    return [
        f"[SOURCE {record.source_id} part={index:0{width}d}/{len(pieces)}]\n"
        f"{piece}\n[/SOURCE {record.source_id}]"
        for index, piece in enumerate(pieces, 1)
    ]


def _cache_key(
    compressor_key: str,
    source_digest: str,
    settings: SemanticCompressionSettings,
) -> str:
    raw = f"{compressor_key}\n{source_digest}\n{settings}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _last_role_index(messages: list[dict[str, Any]], role: str) -> Optional[int]:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == role:
            return index
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
        return dict(parsed) if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _boolean(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in {"true", "1", "yes", "on"}:
            return True
        if value.strip().lower() in {"false", "0", "no", "off"}:
            return False
    return default


def _ratio(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.10, min(0.99, number))


def _positive_int(value: Any, default: int, *, minimum: int = 1, maximum: Optional[int] = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return default
    return min(number, maximum) if maximum is not None else number


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


_DEFAULT_COORDINATOR: Optional[SemanticContextCoordinator] = None


def get_semantic_coordinator() -> SemanticContextCoordinator:
    global _DEFAULT_COORDINATOR
    if _DEFAULT_COORDINATOR is None:
        _DEFAULT_COORDINATOR = SemanticContextCoordinator()
    return _DEFAULT_COORDINATOR
