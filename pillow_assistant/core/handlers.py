"""Request handlers for the event bus.

A handler is ``async def (request, emit) -> None`` where ``emit`` is
``async def (AgentEvent) -> None``. In R0 the only handler is ``LLMHandler``,
which streams a single model completion. In R1 this is replaced/augmented by the
Agent orchestrator, but the bus contract stays the same.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType, SurfaceLevel, SurfaceSpec
from pillow_assistant.core import llm, references
from pillow_assistant.core.context_budget import join_context_and_prompt
from pillow_assistant.core.semantic_context import resolve_compression_profile
from pillow_assistant.core.llm_log import log_llm_call

Emit = Callable[[AgentEvent], Awaitable[None]]


class LLMHandler:
    """Streams one model completion for an AppRequest.

    ``storage`` must expose ``get_model_config(ref) -> dict | None``.
    ``vault`` must expose ``get_secret(name) -> str | None``.
    """

    def __init__(self, storage: Any, vault: Any) -> None:
        self.storage = storage
        self.vault = vault

    async def __call__(self, request: AppRequest, emit: Emit) -> None:
        cfg = self.storage.get_model_config(request.model_ref)
        if cfg is None:
            await emit(AgentEvent(request_id=request.id, type=EventType.ERROR, text="未找到模型配置"))
            return

        api_key = self.vault.get_secret(cfg["display_name"]) if self.vault else None
        extra = llm.parse_extra(cfg.get("extra"))
        semantic_profile = resolve_compression_profile(self.storage, self.vault, cfg)

        # Turn referenced files/folders into bounded prompt context + image attachments.
        context_text, ref_images = references.materialize(request.references)
        prompt = join_context_and_prompt(context_text, request.prompt)
        image_paths = ([request.image_path] if request.image_path else []) + ref_images

        await emit(AgentEvent(request_id=request.id, type=EventType.START))

        provider = cfg.get("provider", "")
        model = cfg.get("model") or ""
        api_base = cfg.get("base_url")

        chunks: list[str] = []
        # Wrap the streaming call in the LLM logger: prompt + aggregated
        # response + latency + errors land in data/logs/llm-YYYYMMDD.jsonl.
        # The holder is mutable; we fill response/usage just before exiting.
        with log_llm_call(
            provider=provider,
            model=model,
            prompt=prompt,
            api_base=api_base,
            image_count=len(image_paths or []),
            extra=extra,
        ) as log_holder:
            async for token in llm.stream_completion(
                provider=provider,
                model=model,
                prompt=prompt,
                api_key=api_key,
                api_base=api_base,
                image_paths=image_paths or None,
                extra=extra,
                semantic_profile=semantic_profile,
            ):
                chunks.append(token)
                # Keep the log's partial_response live so a mid-stream crash
                # still records what was received before the failure.
                log_holder["response"] = "".join(chunks)
                await emit(AgentEvent(request_id=request.id, type=EventType.TOKEN, text=token))

            log_holder["response"] = "".join(chunks)

        body = "".join(chunks)
        await emit(
            AgentEvent(
                request_id=request.id,
                type=EventType.SURFACE,
                surface=SurfaceSpec(level=SurfaceLevel.L4, kind="text", body=body),
            )
        )
        await emit(AgentEvent(request_id=request.id, type=EventType.DONE))
