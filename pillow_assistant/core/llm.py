"""Thin LiteLLM wrapper with non-blocking context-budget management."""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from pillow_assistant.core import context_budget


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


def _managed_request(
    litellm_module,
    *,
    provider: str,
    model: str,
    api_base: Optional[str],
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]],
    completion_extra: dict[str, Any],
    context_config: dict[str, Any],
    emergency: bool = False,
):
    return context_budget.get_context_manager().prepare(
        provider=provider,
        api_base=api_base,
        model=model,
        messages=messages,
        tools=tools,
        model_info=_model_info(litellm_module, resolve_model_string(provider, model)),
        context_config=context_config,
        completion_extra=completion_extra,
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
) -> AsyncIterator[str]:
    """Yield response tokens via LiteLLM, compacting large inputs first."""
    import litellm

    model_string = resolve_model_string(provider, model)
    messages = build_messages(prompt, image_paths)
    completion_extra, context_config = _split_context_config(extra)
    manager = context_budget.get_context_manager()
    managed = _managed_request(
        litellm,
        provider=provider,
        model=model,
        api_base=api_base,
        messages=messages,
        tools=None,
        completion_extra=completion_extra,
        context_config=context_config,
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
        managed = _managed_request(
            litellm,
            provider=provider,
            model=model,
            api_base=api_base,
            messages=messages,
            tools=None,
            completion_extra=completion_extra,
            context_config=context_config,
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
) -> ToolTurn:
    """One non-streaming completion that may request tool calls."""
    import litellm

    model_string = resolve_model_string(provider, model)
    completion_extra, context_config = _split_context_config(extra)
    manager = context_budget.get_context_manager()
    managed = _managed_request(
        litellm,
        provider=provider,
        model=model,
        api_base=api_base,
        messages=messages,
        tools=tools,
        completion_extra=completion_extra,
        context_config=context_config,
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
        managed = _managed_request(
            litellm,
            provider=provider,
            model=model,
            api_base=api_base,
            messages=messages,
            tools=tools,
            completion_extra=completion_extra,
            context_config=context_config,
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
    )
    return turn.content or ""
