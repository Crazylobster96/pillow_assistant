"""Thin LiteLLM wrapper with model-assisted context management."""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from pillow_assistant.core import context_budget, semantic_context


def resolve_model_string(provider: str, model: str) -> str:
    """Map a stored (provider, model) pair to a LiteLLM model string."""
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if not model:
        raise ValueError("model name is empty; set it in the model config")
    if "/" in model:
        return model
    if provider == "ollama":
        return f"ollama/{model}"
    if provider in ("anthropic", "claude"):
        return f"anthropic/{model}"
    return f"openai/{model}"


def encode_image_data_url(path: str | Path) -> str:
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_messages(prompt: str, image_paths: Optional[list[str]] = None) -> list[dict[str, Any]]:
    if image_paths:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in image_paths:
            content.append({"type": "image_url", "image_url": {"url": encode_image_data_url(path)}})
        return [{"role": "user", "content": content}]
    return [{"role": "user", "content": prompt}]


def parse_extra(extra: Optional[str]) -> dict[str, Any]:
    """Parse the optional JSON ``extra`` field; tolerate empty/garbage."""
    if not extra:
        return {}
    try:
        data = json.loads(extra)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _split_context_config(extra: Optional[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove Pillow-only context settings before calling LiteLLM."""
    completion_extra = dict(extra or {})
    raw = completion_extra.pop("pillow_context", {})
    return completion_extra, dict(raw) if isinstance(raw, dict) else {}


def _model_info(litellm_module, model_string: str) -> dict[str, Any]:
    try:
        info = litellm_module.get_model_info(model_string)
        return dict(info) if isinstance(info, dict) else {}
    except Exception:
        return {}


def _usage_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    for method in ("model_dump", "dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                data = fn()
                return dict(data) if isinstance(data, dict) else {}
            except Exception:
                pass
    out: dict[str, Any] = {}
    for name in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "total_tokens"):
        item = getattr(value, name, None)
        if item is not None:
            out[name] = item
    return out


def _actual_input_tokens(usage: dict[str, Any]) -> Optional[int]:
    for name in ("input_tokens", "prompt_tokens"):
        try:
            value = int(usage.get(name) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return None


def _is_context_length_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(pattern in text for pattern in (
        "context_length_exceeded", "maximum context length", "context window",
        "too many tokens", "prompt is too long", "input token count", "request too large",
    ))


def _semantic_profile(
    *,
    provider: str,
    model: str,
    api_base: Optional[str],
    api_key: Optional[str],
    completion_extra: dict[str, Any],
    context_config: dict[str, Any],
    semantic_profile: Optional[dict[str, Any]],
) -> dict[str, Any]:
    raw = context_config.get("semantic_compression", {})
    raw = raw if isinstance(raw, dict) else {}
    runtime = dict(semantic_profile or {})
    compressor_provider = runtime.get("provider") or raw.get("provider") or provider
    compressor_model = runtime.get("model") or raw.get("model") or model
    compressor_base = (
        runtime.get("api_base")
        if "api_base" in runtime
        else raw.get("api_base", api_base)
    )
    same_connection = (
        str(compressor_provider or "").strip().lower() == str(provider or "").strip().lower()
        and str(compressor_base or "").strip() == str(api_base or "").strip()
    )
    if "api_key" in runtime:
        compressor_key = runtime.get("api_key")
    else:
        compressor_key = api_key if same_connection else None

    if isinstance(runtime.get("extra"), dict):
        profile_extra = dict(runtime["extra"])
    elif isinstance(raw.get("extra"), dict):
        profile_extra = dict(raw["extra"])
    elif same_connection:
        profile_extra = dict(completion_extra)
    else:
        profile_extra = {}
    profile_extra, _ = _split_context_config(profile_extra)
    for key in (
        "model", "messages", "stream", "tools", "tool_choice", "api_key", "api_base",
        "max_tokens", "max_completion_tokens", "temperature", "response_format",
    ):
        profile_extra.pop(key, None)
    return {
        "provider": compressor_provider,
        "model": compressor_model,
        "api_base": compressor_base,
        "api_key": compressor_key,
        "extra": profile_extra,
        "profile_key": context_budget.model_profile_key(
            str(compressor_provider), compressor_base, str(compressor_model)
        ),
    }


def _cap_semantic_chunk_size(
    context_config: dict[str, Any], compressor_info: dict[str, Any]
) -> dict[str, Any]:
    """Keep compressor input + output inside the compressor model's own window."""
    config = dict(context_config)
    raw = config.get("semantic_compression", {})
    if raw is False:
        return config
    semantic = dict(raw) if isinstance(raw, dict) else {}
    try:
        known_input = int(
            compressor_info.get("max_input_tokens")
            or compressor_info.get("max_context_tokens")
            or compressor_info.get("max_tokens")
            or 0
        )
    except (TypeError, ValueError):
        known_input = 0
    if known_input > 0:
        try:
            requested_chunk = int(semantic.get("chunk_tokens") or 24_000)
        except (TypeError, ValueError):
            requested_chunk = 24_000
        semantic["chunk_tokens"] = min(
            requested_chunk, max(2_048, int(known_input * 0.55))
        )
        try:
            known_output = int(compressor_info.get("max_output_tokens") or 0)
        except (TypeError, ValueError):
            known_output = 0
        output_cap = known_output or max(512, int(known_input * 0.20))
        try:
            chunk_summary = int(semantic.get("chunk_summary_tokens") or 2_048)
            final_summary = int(semantic.get("summary_max_tokens") or 6_000)
        except (TypeError, ValueError):
            chunk_summary, final_summary = 2_048, 6_000
        semantic["chunk_summary_tokens"] = min(chunk_summary, max(512, output_cap))
        semantic["summary_max_tokens"] = min(final_summary, max(768, output_cap))
        config["semantic_compression"] = semantic
    return config


async def _managed_request(
    litellm_module,
    *,
    provider: str,
    model: str,
    api_key: Optional[str],
    api_base: Optional[str],
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]],
    completion_extra: dict[str, Any],
    context_config: dict[str, Any],
    semantic_profile: Optional[dict[str, Any]] = None,
    emergency: bool = False,
):
    profile = _semantic_profile(
        provider=provider,
        model=model,
        api_base=api_base,
        api_key=api_key,
        completion_extra=completion_extra,
        context_config=context_config,
        semantic_profile=semantic_profile,
    )
    compressor_model_string = resolve_model_string(profile["provider"], profile["model"])
    compressor_info = _model_info(litellm_module, compressor_model_string)
    managed_context_config = _cap_semantic_chunk_size(context_config, compressor_info)

    async def semantic_completion(
        compressor_messages: list[dict[str, Any]], max_tokens: int
    ) -> str:
        kwargs = dict(profile["extra"])
        kwargs.update({
            "model": compressor_model_string,
            "messages": compressor_messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens,
        })
        if profile.get("api_key"):
            kwargs["api_key"] = profile["api_key"]
        if profile.get("api_base"):
            kwargs["api_base"] = profile["api_base"]
        response = await litellm_module.acompletion(**kwargs)
        content = getattr(response.choices[0].message, "content", "")
        if isinstance(content, list):
            return "".join(
                str(block.get("text") or "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(content or "")

    model_string = resolve_model_string(provider, model)
    return await semantic_context.get_semantic_coordinator().manage(
        manager=context_budget.get_context_manager(),
        provider=provider,
        api_base=api_base,
        model=model,
        messages=messages,
        tools=tools,
        model_info=_model_info(litellm_module, model_string),
        context_config=managed_context_config,
        completion_extra=completion_extra,
        compressor_key=profile["profile_key"],
        completion_call=semantic_completion,
        emergency=emergency,
    )


async def stream_completion(
    *,
    provider: str,
    model: str,
    prompt: str,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    image_paths: Optional[list[str]] = None,
    extra: Optional[dict[str, Any]] = None,
    semantic_profile: Optional[dict[str, Any]] = None,
) -> AsyncIterator[str]:
    """Yield response tokens via LiteLLM, semantically compressing large inputs first."""
    import litellm

    model_string = resolve_model_string(provider, model)
    messages = build_messages(prompt, image_paths)
    completion_extra, context_config = _split_context_config(extra)
    manager = context_budget.get_context_manager()
    managed = await _managed_request(
        litellm,
        provider=provider,
        model=model,
        api_key=api_key,
        api_base=api_base,
        messages=messages,
        tools=None,
        completion_extra=completion_extra,
        context_config=context_config,
        semantic_profile=semantic_profile,
    )
    kwargs: dict[str, Any] = {"model": model_string, "messages": managed.messages, "stream": True}
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if completion_extra:
        kwargs.update(completion_extra)

    try:
        response = await litellm.acompletion(**kwargs)
    except Exception as exc:
        if not _is_context_length_error(exc):
            raise
        managed = await _managed_request(
            litellm,
            provider=provider,
            model=model,
            api_key=api_key,
            api_base=api_base,
            messages=managed.messages,
            tools=None,
            completion_extra=completion_extra,
            context_config=context_config,
            semantic_profile=semantic_profile,
            emergency=True,
        )
        kwargs["messages"] = managed.messages
        response = await litellm.acompletion(**kwargs)

    usage: dict[str, Any] = {}
    async for chunk in response:
        chunk_usage = _usage_dict(getattr(chunk, "usage", None))
        if chunk_usage:
            usage = chunk_usage
        try:
            token = chunk.choices[0].delta.content
        except (AttributeError, IndexError):
            token = None
        if token:
            yield token
    manager.observe(managed, _actual_input_tokens(usage))


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ToolTurn:
    content: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    context_stats: dict[str, Any] = field(default_factory=dict)
    managed_messages: Optional[list[dict[str, Any]]] = None


async def complete_with_tools(
    *,
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    semantic_profile: Optional[dict[str, Any]] = None,
) -> ToolTurn:
    """One non-streaming completion that may request tool calls."""
    import litellm

    model_string = resolve_model_string(provider, model)
    completion_extra, context_config = _split_context_config(extra)
    manager = context_budget.get_context_manager()
    managed = await _managed_request(
        litellm,
        provider=provider,
        model=model,
        api_key=api_key,
        api_base=api_base,
        messages=messages,
        tools=tools,
        completion_extra=completion_extra,
        context_config=context_config,
        semantic_profile=semantic_profile,
    )
    kwargs: dict[str, Any] = {"model": model_string, "messages": managed.messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if completion_extra:
        kwargs.update(completion_extra)

    try:
        response = await litellm.acompletion(**kwargs)
    except Exception as exc:
        if not _is_context_length_error(exc):
            raise
        managed = await _managed_request(
            litellm,
            provider=provider,
            model=model,
            api_key=api_key,
            api_base=api_base,
            messages=managed.messages,
            tools=tools,
            completion_extra=completion_extra,
            context_config=context_config,
            semantic_profile=semantic_profile,
            emergency=True,
        )
        kwargs["messages"] = managed.messages
        response = await litellm.acompletion(**kwargs)

    message = response.choices[0].message
    calls: list[ToolCall] = []
    for tc in (getattr(message, "tool_calls", None) or []):
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}"))
    usage = _usage_dict(getattr(response, "usage", None))
    manager.observe(managed, _actual_input_tokens(usage))
    return ToolTurn(
        content=getattr(message, "content", None),
        tool_calls=calls,
        usage=usage,
        context_stats=managed.diagnostics(),
        managed_messages=managed.messages,
    )


async def complete(
    *,
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    semantic_profile: Optional[dict[str, Any]] = None,
) -> str:
    """Single non-streaming completion returning plain text (no tools)."""
    turn = await complete_with_tools(
        provider=provider,
        model=model,
        messages=messages,
        tools=None,
        api_key=api_key,
        api_base=api_base,
        extra=extra,
        semantic_profile=semantic_profile,
    )
    return turn.content or ""
