"""Fast context estimation, compaction, and per-model calibration.

The hot path deliberately uses a cheap deterministic estimate.  Exact usage
reported by the provider is observed *after* a completion and calibrates later
requests, so context management never needs an extra blocking model request.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from pillow_assistant.core.data_paths import user_data_dir


CHINESE_TOKENS_PER_CHAR = 0.55
ENGLISH_TOKENS_PER_WORD = 1.10
OTHER_TOKENS_PER_CHAR = 1.0
MESSAGE_OVERHEAD_TOKENS = 8
CONTENT_BLOCK_OVERHEAD_TOKENS = 4
IMAGE_ESTIMATE_TOKENS = 1600

DEFAULT_CONTEXT_WINDOW = 16_384
DEFAULT_OUTPUT_RESERVE = 4_096
DEFAULT_SOFT_RATIO = 0.85
DEFAULT_TARGET_RATIO = 0.72
EMERGENCY_TARGET_RATIO = 0.55

CONTEXT_OPEN = "<pillow_supporting_context>"
CONTEXT_CLOSE = "</pillow_supporting_context>"
REQUEST_OPEN = "<pillow_current_request>"
REQUEST_CLOSE = "</pillow_current_request>"
PROJECT_STATE_OPEN = "<pillow_project_state>"
PROJECT_STATE_CLOSE = "</pillow_project_state>"
PROJECT_EVIDENCE_OPEN = "<pillow_project_memory_evidence>"
PROJECT_EVIDENCE_CLOSE = "</pillow_project_memory_evidence>"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")


@dataclass
class TextFeatures:
    chinese_chars: int = 0
    english_words: int = 0
    other_nonspace_chars: int = 0
    messages: int = 0
    content_blocks: int = 0
    images: int = 0

    def add(self, other: "TextFeatures") -> None:
        for name in asdict(self):
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class TokenEstimate:
    base_tokens: int
    adjusted_tokens: int
    correction_factor: float
    uncertainty_factor: float
    features: TextFeatures = field(default_factory=TextFeatures)


@dataclass
class ContextLimits:
    hard_input_tokens: int
    soft_input_tokens: int
    target_input_tokens: int
    output_reserve_tokens: int
    source: str = "default"


@dataclass
class ContextManagementResult:
    messages: list[dict[str, Any]]
    estimate_before: TokenEstimate
    estimate_after: TokenEstimate
    limits: ContextLimits
    model_key: str
    compacted: bool = False
    dropped_rounds: int = 0
    shortened_messages: int = 0
    emergency: bool = False
    semantic_compressed: bool = False
    semantic_verified: bool = False
    semantic_status: str = "not-requested"
    semantic_source_tokens: int = 0
    semantic_source_count: int = 0
    semantic_chunk_count: int = 0

    def diagnostics(self) -> dict[str, Any]:
        return {
            "estimated_before": self.estimate_before.adjusted_tokens,
            "estimated_after": self.estimate_after.adjusted_tokens,
            "hard_input_tokens": self.limits.hard_input_tokens,
            "soft_input_tokens": self.limits.soft_input_tokens,
            "target_input_tokens": self.limits.target_input_tokens,
            "compacted": self.compacted,
            "dropped_rounds": self.dropped_rounds,
            "shortened_messages": self.shortened_messages,
            "emergency": self.emergency,
            "semantic_compressed": self.semantic_compressed,
            "semantic_verified": self.semantic_verified,
            "semantic_status": self.semantic_status,
            "semantic_source_tokens": self.semantic_source_tokens,
            "semantic_source_count": self.semantic_source_count,
            "semantic_chunk_count": self.semantic_chunk_count,
        }


def join_context_and_prompt(context: str, prompt: str) -> str:
    """Mark supporting context separately so it can be compacted safely."""
    if not context:
        return prompt
    return (
        f"{CONTEXT_OPEN}\n{context}\n{CONTEXT_CLOSE}\n\n"
        f"{REQUEST_OPEN}\n{prompt}\n{REQUEST_CLOSE}"
    )


def model_profile_key(provider: str, api_base: Optional[str], model: str) -> str:
    raw = "\n".join(((provider or "").strip().lower(), (api_base or "").strip(), (model or "").strip()))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{(provider or 'unknown').strip().lower()}:{(model or 'unknown').strip()}:{digest}"


def text_features(text: str) -> TextFeatures:
    text = text or ""
    chinese = len(_CJK_RE.findall(text))
    without_cjk = _CJK_RE.sub("", text)
    words = len(_ENGLISH_WORD_RE.findall(without_cjk))
    remainder = _ENGLISH_WORD_RE.sub("", without_cjk)
    other = sum(1 for char in remainder if not char.isspace())
    return TextFeatures(chinese_chars=chinese, english_words=words, other_nonspace_chars=other)


def estimate_text_tokens(text: str) -> int:
    features = text_features(text)
    estimate = (
        features.chinese_chars * CHINESE_TOKENS_PER_CHAR
        + features.english_words * ENGLISH_TOKENS_PER_WORD
        + features.other_nonspace_chars * OTHER_TOKENS_PER_CHAR
    )
    return max(0, math.ceil(estimate - 1e-9))


class TokenCalibrationStore:
    """Persist only aggregate ratios; raw prompts never leave the request path."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path is not None else user_data_dir() / "token_calibration.json"
        self._lock = threading.RLock()
        self._loaded = False
        self._profiles: dict[str, dict[str, Any]] = {}

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                data = json.loads(self.path.read_text("utf-8"))
                profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
                self._profiles = profiles if isinstance(profiles, dict) else {}
            except (OSError, TypeError, ValueError):
                self._profiles = {}

    def profile(self, key: str) -> dict[str, Any]:
        self._load()
        with self._lock:
            return dict(self._profiles.get(key, {}))

    def factors(self, key: str) -> tuple[float, float]:
        profile = self.profile(key)
        samples = max(0, int(profile.get("sample_count", 0) or 0))
        ratios = [float(value) for value in profile.get("recent_ratios", []) if _positive_number(value)]
        ewma = float(profile.get("ewma_ratio", 1.0) or 1.0)
        p90 = _percentile(ratios, 0.90) if ratios else 1.0
        # The agreed base formula is a lower-bound starting point.  Never
        # calibrate below it automatically; unused headroom is safer than an
        # optimistic estimate for a custom gateway.
        correction = max(1.0, ewma, p90)
        if samples == 0:
            uncertainty = 1.30
        elif samples < 5:
            uncertainty = 1.20
        elif samples < 20:
            uncertainty = 1.10
        else:
            uncertainty = 1.05
        return correction, uncertainty

    def observe(self, key: str, base_estimate: int, actual_input_tokens: int) -> None:
        if base_estimate <= 0 or actual_input_tokens <= 0:
            return
        ratio = max(0.25, min(4.0, actual_input_tokens / base_estimate))
        self._load()
        with self._lock:
            current = dict(self._profiles.get(key, {}))
            samples = max(0, int(current.get("sample_count", 0) or 0))
            old_ewma = float(current.get("ewma_ratio", ratio) or ratio)
            ewma = ratio if samples == 0 else old_ewma * 0.8 + ratio * 0.2
            recent = [float(value) for value in current.get("recent_ratios", []) if _positive_number(value)]
            recent = (recent + [round(ratio, 6)])[-50:]
            self._profiles[key] = {
                "sample_count": samples + 1,
                "ewma_ratio": round(ewma, 6),
                "recent_ratios": recent,
            }
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps({"version": 1, "profiles": self._profiles}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            # Calibration is an optimization; a read-only data directory must
            # never prevent a completion.
            try:
                temporary.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass


class ContextBudgetManager:
    def __init__(self, calibration: Optional[TokenCalibrationStore] = None) -> None:
        self.calibration = calibration or TokenCalibrationStore()

    def resolve_limits(
        self,
        *,
        model_info: Optional[dict[str, Any]] = None,
        context_config: Optional[dict[str, Any]] = None,
        completion_extra: Optional[dict[str, Any]] = None,
    ) -> ContextLimits:
        info = model_info or {}
        config = context_config or {}
        extra = completion_extra or {}

        explicit_input = _positive_int(config.get("max_input_tokens"))
        known_input = _positive_int(info.get("max_input_tokens"))
        context_window = (
            _positive_int(config.get("context_window"))
            or _positive_int(info.get("max_tokens"))
            or _positive_int(info.get("max_context_tokens"))
            or DEFAULT_CONTEXT_WINDOW
        )
        output_reserve = (
            _positive_int(config.get("output_reserve_tokens"))
            or _positive_int(extra.get("max_completion_tokens"))
            or _positive_int(extra.get("max_tokens"))
            or min(DEFAULT_OUTPUT_RESERVE, max(512, context_window // 4))
        )
        if explicit_input:
            hard = explicit_input
            source = "config:max_input_tokens"
        elif known_input:
            hard = known_input
            source = "provider:max_input_tokens"
        else:
            hard = max(1024, context_window - output_reserve)
            source = "config:context_window" if config.get("context_window") else "model/default:context_window"

        soft_ratio = _bounded_ratio(config.get("soft_ratio"), DEFAULT_SOFT_RATIO)
        target_ratio = _bounded_ratio(config.get("target_ratio"), DEFAULT_TARGET_RATIO)
        target_ratio = min(target_ratio, soft_ratio - 0.02)
        return ContextLimits(
            hard_input_tokens=hard,
            soft_input_tokens=max(512, int(hard * soft_ratio)),
            target_input_tokens=max(384, int(hard * target_ratio)),
            output_reserve_tokens=output_reserve,
            source=source,
        )

    def estimate(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        model_key: str,
    ) -> TokenEstimate:
        features = TextFeatures(messages=len(messages))
        base = len(messages) * MESSAGE_OVERHEAD_TOKENS
        for message in messages:
            message_features, message_tokens = _estimate_message(message)
            features.add(message_features)
            base += message_tokens
        if tools:
            serialized = json.dumps(tools, ensure_ascii=False, separators=(",", ":"), default=str)
            tool_features = text_features(serialized)
            features.add(tool_features)
            base += estimate_text_tokens(serialized) + 16 * len(tools)
        correction, uncertainty = self.calibration.factors(model_key)
        adjusted = math.ceil(base * correction * uncertainty)
        return TokenEstimate(
            base_tokens=max(1, base),
            adjusted_tokens=max(1, adjusted),
            correction_factor=correction,
            uncertainty_factor=uncertainty,
            features=features,
        )

    def prepare(
        self,
        *,
        provider: str,
        api_base: Optional[str],
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model_info: Optional[dict[str, Any]] = None,
        context_config: Optional[dict[str, Any]] = None,
        completion_extra: Optional[dict[str, Any]] = None,
        emergency: bool = False,
    ) -> ContextManagementResult:
        key = model_profile_key(provider, api_base, model)
        limits = self.resolve_limits(
            model_info=model_info,
            context_config=context_config,
            completion_extra=completion_extra,
        )
        before = self.estimate(messages, tools, key)
        should_compact = emergency or before.adjusted_tokens >= limits.soft_input_tokens
        if not should_compact:
            return ContextManagementResult(
                messages=messages,
                estimate_before=before,
                estimate_after=before,
                limits=limits,
                model_key=key,
            )

        target = limits.target_input_tokens
        if emergency:
            target = min(target, int(limits.hard_input_tokens * EMERGENCY_TARGET_RATIO))
            target = min(target, max(384, int(before.adjusted_tokens * EMERGENCY_TARGET_RATIO)))
        compacted, dropped, shortened = self._compact_messages(messages, tools, key, target)
        after = self.estimate(compacted, tools, key)
        return ContextManagementResult(
            messages=compacted,
            estimate_before=before,
            estimate_after=after,
            limits=limits,
            model_key=key,
            compacted=True,
            dropped_rounds=dropped,
            shortened_messages=shortened,
            emergency=emergency,
        )

    def observe(self, result: ContextManagementResult, actual_input_tokens: Optional[int]) -> None:
        if actual_input_tokens:
            self.calibration.observe(
                result.model_key,
                result.estimate_after.base_tokens,
                int(actual_input_tokens),
            )

    def _compact_messages(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        model_key: str,
        target_tokens: int,
    ) -> tuple[list[dict[str, Any]], int, int]:
        working = copy.deepcopy(messages)
        dropped = 0
        shortened = 0

        # First make old verbose messages cheap while retaining their beginning
        # and conclusion.  The current user request is handled only after all
        # lower-priority material has been reduced.
        last_user = _last_role_index(working, "user")
        for index, message in enumerate(working):
            if index == last_user or message.get("role") == "system":
                continue
            limit = 800 if message.get("role") == "tool" else 1400
            shortened += _shorten_message_content(message, limit)

        # Drop complete oldest user rounds, never isolated tool results.  Keep
        # the newest round because it contains the active tool-call chain.
        while self.estimate(working, tools, model_key).adjusted_tokens > target_tokens:
            rounds = _conversation_rounds(working)
            if len(rounds) <= 1:
                break
            remove = set(rounds[0])
            working = [message for index, message in enumerate(working) if index not in remove]
            dropped += 1

        # Compact old tool results in the active round while preserving the
        # most recent one verbatim for immediate reasoning.
        tool_indices = [index for index, message in enumerate(working) if message.get("role") == "tool"]
        for index in tool_indices[:-1]:
            shortened += _shorten_message_content(working[index], 600)

        # Supporting context is explicitly marked by the Agent.  Reduce that
        # before touching the user's current request.
        if self.estimate(working, tools, model_key).adjusted_tokens > target_tokens:
            last_user = _last_role_index(working, "user")
            if last_user is not None:
                shortened += _shorten_supporting_context(working[last_user], 0.45)

        # Deterministic final fitting: repeatedly halve the largest remaining
        # compressible text.  System prompts and the current request are last.
        for include_protected in (False, True):
            for _ in range(24):
                if self.estimate(working, tools, model_key).adjusted_tokens <= target_tokens:
                    break
                candidate = _largest_text_candidate(working, include_protected=include_protected)
                if candidate is None:
                    break
                index, block_index, length = candidate
                new_limit = max(160, int(length * 0.55))
                changed = _shorten_message_content(working[index], new_limit, block_index=block_index)
                if not changed:
                    break
                shortened += changed

        return working, dropped, shortened


def _estimate_message(message: dict[str, Any]) -> tuple[TextFeatures, int]:
    features = TextFeatures()
    tokens = 0
    for key in ("role", "name", "tool_call_id"):
        value = message.get(key)
        if value:
            value_features = text_features(str(value))
            features.add(value_features)
            tokens += estimate_text_tokens(str(value))
    content = message.get("content")
    if isinstance(content, str):
        value_features = text_features(content)
        features.add(value_features)
        tokens += estimate_text_tokens(content)
    elif isinstance(content, list):
        for block in content:
            features.content_blocks += 1
            tokens += CONTENT_BLOCK_OVERHEAD_TOKENS
            if not isinstance(block, dict):
                value = str(block)
                features.add(text_features(value))
                tokens += estimate_text_tokens(value)
                continue
            if isinstance(block.get("text"), str):
                value = block["text"]
                features.add(text_features(value))
                tokens += estimate_text_tokens(value)
            if block.get("type") in {"image", "image_url", "input_image"}:
                features.images += 1
                tokens += IMAGE_ESTIMATE_TOKENS
    if message.get("tool_calls"):
        serialized = json.dumps(message["tool_calls"], ensure_ascii=False, separators=(",", ":"), default=str)
        features.add(text_features(serialized))
        tokens += estimate_text_tokens(serialized)
    return features, tokens


def _conversation_rounds(messages: list[dict[str, Any]]) -> list[list[int]]:
    rounds: list[list[int]] = []
    current: list[int] = []
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            continue
        if message.get("role") == "user" and current:
            rounds.append(current)
            current = []
        current.append(index)
    if current:
        rounds.append(current)
    return rounds


def _last_role_index(messages: list[dict[str, Any]], role: str) -> Optional[int]:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == role:
            return index
    return None


def _shorten_message_content(message: dict[str, Any], limit: int, block_index: Optional[int] = None) -> int:
    content = message.get("content")
    if isinstance(content, str):
        shortened = _shorten_text(content, limit)
        if shortened != content:
            message["content"] = shortened
            return 1
        return 0
    if not isinstance(content, list):
        return 0
    candidates = range(len(content)) if block_index is None else (block_index,)
    changed = 0
    for index in candidates:
        if index < 0 or index >= len(content):
            continue
        block = content[index]
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        shortened = _shorten_text(block["text"], limit)
        if shortened != block["text"]:
            block["text"] = shortened
            changed += 1
    return changed


def _shorten_supporting_context(message: dict[str, Any], keep_ratio: float) -> int:
    content = message.get("content")
    if isinstance(content, str):
        shortened = _compact_marked_context(content, keep_ratio)
        if shortened != content:
            message["content"] = shortened
            return 1
        return 0
    if isinstance(content, list):
        changed = 0
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                shortened = _compact_marked_context(block["text"], keep_ratio)
                if shortened != block["text"]:
                    block["text"] = shortened
                    changed += 1
        return changed
    return 0


def _compact_marked_context(text: str, keep_ratio: float) -> str:
    start = text.find(CONTEXT_OPEN)
    end = text.find(CONTEXT_CLOSE)
    if start < 0 or end <= start:
        return text
    body_start = start + len(CONTEXT_OPEN)
    context = text[body_start:end].strip("\n")
    state_start = context.find(PROJECT_STATE_OPEN)
    state_end = context.find(PROJECT_STATE_CLOSE)
    limit = max(240, int(len(context) * keep_ratio))
    if state_start >= 0 and state_end > state_start:
        state_end += len(PROJECT_STATE_CLOSE)
        state_block = context[state_start:state_end]
        supporting = (context[:state_start] + context[state_end:]).strip()
        supporting_limit = max(80, limit - len(state_block) - 2)
        compacted_supporting = _shorten_text(supporting, supporting_limit)
        compacted = state_block
        if compacted_supporting:
            compacted += "\n\n" + compacted_supporting
    else:
        compacted = _shorten_text(context, limit)
    return text[:body_start] + "\n" + compacted + "\n" + text[end:]


def _largest_text_candidate(
    messages: list[dict[str, Any]], *, include_protected: bool
) -> Optional[tuple[int, Optional[int], int]]:
    last_user = _last_role_index(messages, "user")
    candidates: list[tuple[int, Optional[int], int]] = []
    for index, message in enumerate(messages):
        if not include_protected and (message.get("role") == "system" or index == last_user):
            continue
        content = message.get("content")
        if isinstance(content, str) and len(content) > 240:
            candidates.append((index, None, len(content)))
        elif isinstance(content, list):
            for block_index, block in enumerate(content):
                if isinstance(block, dict) and isinstance(block.get("text"), str) and len(block["text"]) > 240:
                    candidates.append((index, block_index, len(block["text"])))
    return max(candidates, key=lambda item: item[2]) if candidates else None


def _shorten_text(text: str, limit: int) -> str:
    if len(text) <= limit or limit < 80:
        return text
    state_start = text.find(PROJECT_STATE_OPEN)
    state_end = text.find(PROJECT_STATE_CLOSE)
    if state_start >= 0 and state_end > state_start:
        state_end += len(PROJECT_STATE_CLOSE)
        state_block = text[state_start:state_end]
        remainder = (text[:state_start] + text[state_end:]).strip()
        if not remainder:
            return state_block
        remainder_limit = max(80, limit - len(state_block) - 2)
        compacted_remainder = _shorten_text(remainder, remainder_limit)
        return state_block + ("\n\n" + compacted_remainder if compacted_remainder else "")
    marker = f"\n… [Pillow compacted {len(text) - limit} characters] …\n"
    available = max(20, limit - len(marker))
    head = max(10, int(available * 0.68))
    tail = max(10, available - head)
    return text[:head] + marker + text[-tail:]


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _bounded_ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(0.98, max(0.10, parsed))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 1.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


_DEFAULT_MANAGER: Optional[ContextBudgetManager] = None
_DEFAULT_MANAGER_LOCK = threading.Lock()


def get_context_manager() -> ContextBudgetManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        with _DEFAULT_MANAGER_LOCK:
            if _DEFAULT_MANAGER is None:
                _DEFAULT_MANAGER = ContextBudgetManager()
    return _DEFAULT_MANAGER
