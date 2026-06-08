"""Thin LiteLLM wrapper.

Pure helpers (model-string + message building) are kept importable without
LiteLLM installed so they can be unit-tested in isolation. The network call
(``stream_completion``) imports litellm lazily.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional


def resolve_model_string(provider: str, model: str) -> str:
    """Map a stored (provider, model) pair to a LiteLLM model string.

    OpenAI / vLLM / 自定义 are all OpenAI-compatible and routed via the
    ``openai/`` prefix together with ``api_base``. Ollama gets its own prefix.
    """
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if not model:
        raise ValueError("model name is empty; set it in the model config")
    if "/" in model:
        # Caller already qualified the provider (e.g. "openai/gpt-4o").
        return model
    if provider == "ollama":
        return f"ollama/{model}"
    if provider in ("anthropic", "claude"):
        # Anthropic Messages API (incl. Anthropic-format gateways via api_base).
        return f"anthropic/{model}"
    # OpenAI, vLLM, 自定义, and any OpenAI-compatible endpoint.
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
            content.append(
                {"type": "image_url", "image_url": {"url": encode_image_data_url(path)}}
            )
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
    """Yield response tokens from the configured model via LiteLLM streaming."""
    import litellm  # lazy import: keeps pure helpers testable without the dep

    model_string = resolve_model_string(provider, model)
    messages = build_messages(prompt, image_paths)
    kwargs: dict[str, Any] = {"model": model_string, "messages": messages, "stream": True}
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if extra:
        kwargs.update(extra)

    response = await litellm.acompletion(**kwargs)
    async for chunk in response:
        try:
            token = chunk.choices[0].delta.content
        except (AttributeError, IndexError):
            token = None
        if token:
            yield token


# -- tool calling (R1) ------------------------------------------------------
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as returned by the model


@dataclass
class ToolTurn:
    content: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)


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
    """One non-streaming completion that may request tool calls.

    Returns a normalized :class:`ToolTurn` so the Agent loop stays independent
    of LiteLLM's response objects (and is unit-testable with a fake).
    """
    import litellm  # lazy import

    kwargs: dict[str, Any] = {"model": resolve_model_string(provider, model), "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if extra:
        kwargs.update(extra)

    response = await litellm.acompletion(**kwargs)
    message = response.choices[0].message
    calls: list[ToolCall] = []
    for tc in (getattr(message, "tool_calls", None) or []):
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}"))
    return ToolTurn(content=getattr(message, "content", None), tool_calls=calls)


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
        provider=provider, model=model, messages=messages, tools=None,
        api_key=api_key, api_base=api_base, extra=extra,
    )
    return turn.content or ""
