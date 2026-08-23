"""Multi-step tool-calling Agent loop (T0): driven by a pluggable ToolRegistry.

The loop hands the registry's tool schemas to the model each step and dispatches
any tool calls back through the registry, so adding a tool needs no change here.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable, Optional

from pillow_assistant.contracts import AgentEvent, EventType, SurfaceLevel, SurfaceSpec
from pillow_assistant.core.context_budget import join_context_and_prompt
from pillow_assistant.core import llm
from pillow_assistant.core.agent.prompts import SYSTEM_PROMPT
from pillow_assistant.core.i18n import t

Emit = Callable[[AgentEvent], Awaitable[None]]


class ToolLoopAgent:
    def __init__(self, cfg: dict, api_key: Optional[str], registry, ctx, max_steps: int = 6) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self.registry = registry
        self.ctx = ctx
        self.max_steps = max_steps

    async def run(
        self,
        *,
        prompt: str,
        emit: Emit,
        request_id: str,
        context: str = "",
        image_paths: Optional[list[str]] = None,
        history: Optional[list[dict]] = None,
        resume_messages: Optional[list[dict]] = None,
    ) -> str:
        provider = self.cfg.get("provider", "")
        model = self.cfg.get("model") or ""
        api_base = self.cfg.get("base_url")
        extra = llm.parse_extra(self.cfg.get("extra"))
        tools = self.registry.schemas()

        user_text = join_context_and_prompt(context, prompt)
        if resume_messages:
            # Continue an interrupted run (hit the step limit last time): keep
            # the full transcript incl. tool calls/results, append the new ask.
            messages: list[dict[str, Any]] = list(resume_messages)
            messages.append({"role": "user", "content": user_text})
        else:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            if history:
                messages.extend({"role": t["role"], "content": t["content"]} for t in history)
            if image_paths:
                content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
                for p in image_paths:
                    content.append({"type": "image_url", "image_url": {"url": llm.encode_image_data_url(p)}})
                messages.append({"role": "user", "content": content})
            else:
                messages.append({"role": "user", "content": user_text})

        final_text = ""
        reached_limit = True
        used_tools = False
        self._artifacts: list[str] = []
        self._step = 0
        for step in range(1, self.max_steps + 1):
            self._step = step
            turn = await llm.complete_with_tools(
                provider=provider, model=model, messages=messages, tools=tools,
                api_key=self.api_key, api_base=api_base, extra=extra,
            )
            if getattr(turn, "managed_messages", None) is not None:
                messages = list(turn.managed_messages)
            if not turn.tool_calls:
                final_text = turn.content or ""
                if final_text:
                    if used_tools:
                        # Visually separate the answer from the tool-call noise.
                        await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN,
                                              text=t("core.answer_sep")))
                    await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN, text=final_text))
                reached_limit = False
                break
            used_tools = True

            messages.append({
                "role": "assistant",
                "content": turn.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in turn.tool_calls
                ],
            })
            for tc in turn.tool_calls:
                result_text = await self._run_tool(tc, emit, request_id)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

        # Expose the run's end state so the orchestrator can offer "继续":
        # on limit it stores the transcript and resumes from it next request.
        self.reached_limit = reached_limit
        self.final_messages = list(messages)

        if reached_limit:
            note = t("core.max_steps")
            final_text += note
            await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN, text=note))

        from pillow_assistant.core.surface_router import route
        level = route(final_text, self._artifacts)
        await emit(AgentEvent(
            request_id=request_id, type=EventType.SURFACE,
            surface=SurfaceSpec(level=level, kind="text", body=final_text,
                                payload={"artifacts": self._artifacts,
                                         "workspace": str(getattr(self.ctx, "workspace", ""))}),
        ))
        await emit(AgentEvent(request_id=request_id, type=EventType.DONE))
        return final_text

    async def _run_tool(self, tc, emit: Emit, request_id: str) -> str:
        try:
            args = json.loads(tc.arguments or "{}")
        except ValueError:
            args = {}
        step = getattr(self, "_step", 1)
        await emit(AgentEvent(
            request_id=request_id,
            type=EventType.TOOL_START,
            text=tc.name,
            meta={"name": tc.name, "step": step, "total": self.max_steps},
        ))
        await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN,
                              text=t("loop.step", k=step, n=self.max_steps, name=tc.name)))
        t0 = time.time()
        result = await self.registry.dispatch(tc.name, args, self.ctx)
        summary = result.text
        await emit(AgentEvent(
            request_id=request_id,
            type=EventType.TOOL_RESULT,
            text=summary,
            meta={
                "name": tc.name,
                "step": step,
                "total": self.max_steps,
                "ok": bool(getattr(result, "ok", False)),
            },
        ))
        self._artifacts.extend(getattr(result, "artifacts", None) or [])
        audit = getattr(self.ctx, "audit", None)
        if audit is not None:
            audit.tool_call(tc.name, args, getattr(result, "ok", None),
                            int((time.time() - t0) * 1000), len(summary))
        token = getattr(result, "undo_token", None)
        if token:
            await emit(AgentEvent(request_id=request_id, type=EventType.UNDO,
                                  text=getattr(result, "undo_label", "") or t("undo.default_label"),
                                  meta={"token": token}))
        brief = summary if len(summary) <= 800 else summary[:800] + "…"
        await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN, text=brief + "\n"))
        return summary
