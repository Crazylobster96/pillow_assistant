"""Multi-step tool-calling Agent loop (T0): driven by a pluggable ToolRegistry.

The loop hands the registry's tool schemas to the model each step and dispatches
any tool calls back through the registry, so adding a tool needs no change here.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Awaitable, Callable, Optional

from pillow_assistant.contracts import AgentEvent, EventType, SurfaceLevel, SurfaceSpec
from pillow_assistant.core.context_budget import join_context_and_prompt
from pillow_assistant.core import llm
from pillow_assistant.core.agent import text_tool_calls
from pillow_assistant.core.semantic_context import resolve_compression_profile
from pillow_assistant.core.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_METADATA
from pillow_assistant.core.i18n import t

Emit = Callable[[AgentEvent], Awaitable[None]]

# Images a tool hands back (file_read on a render) are shown to vision models in a
# user message after the tool results, at most a few per step, and dropped again
# after a couple of model calls: each one costs about a thousand tokens per call.
IMAGES_PER_STEP = 4
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_KEEP_STEPS = 2


def _seconds(value: float) -> str:
    value = int(round(value))
    return f"{value // 60}:{value % 60:02d}" if value >= 60 else f"{value}s"


def _thousands(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)
# Tools whose result is instructions or reference text for the model, not news
# for the user: the dialog shows one line instead of the first 800 characters.
QUIET_TOOLS = {"apply_skill", "request_project_memory"}
# A tool call written as text that cannot be read: ask for a real call this many times.
TEXT_CALL_NUDGES = 2
# An empty answer is not a finished task: ask the model to carry on this many times.
EMPTY_ANSWER_NUDGES = 2


def _quiet_line(name: str, args: dict, ok: bool) -> str:
    detail = args.get("name") or args.get("path") or args.get("query") or ""
    return t("loop.quiet_result" if ok else "loop.quiet_failed", name=name, detail=str(detail)[:80])


class ToolLoopAgent:
    def __init__(self, cfg: dict, api_key: Optional[str], registry, ctx, max_steps: int = 6) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self.registry = registry
        self.ctx = ctx
        self.max_steps = max_steps

        self.tool_evidence: list[dict[str, Any]] = []
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
        skill: str = "",
    ) -> str:
        provider = self.cfg.get("provider", "")
        model = self.cfg.get("model") or ""
        api_base = self.cfg.get("base_url")
        extra = llm.parse_extra(self.cfg.get("extra"))
        mode = "project" if getattr(self.ctx, "project_id", None) else "chat"
        try:
            tools = self.registry.schemas(mode=mode)
        except TypeError:  # compatibility with minimal test/plugin registries
            tools = self.registry.schemas()
        self.capability_snapshot = {
            "prompts": [dict(SYSTEM_PROMPT_METADATA)],
            "tools": (self.registry.snapshot(mode) if hasattr(self.registry, "snapshot") else []),
            "skills": list(getattr(self.ctx, "skill_snapshot", None) or []),
        }
        audit = getattr(self.ctx, "audit", None)
        if audit is not None and hasattr(audit, "_write"):
            audit._write({"kind": "capability_snapshot", **self.capability_snapshot})
        semantic_profile = resolve_compression_profile(
            getattr(self.ctx, "storage", None),
            getattr(self.ctx, "vault", None),
            self.cfg,
        )

        skill_only = set(getattr(self.registry, "skill_only", None) or ())

        def offered() -> list:
            active = getattr(self.ctx, "active_tools", None) or set()
            return [s for s in tools
                    if s.get("function", {}).get("name") not in skill_only - active]

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
        self.tool_evidence: list[dict[str, Any]] = []
        self._step = 0
        self.last_managed_messages: Optional[list[dict[str, Any]]] = None
        self._reactivate_skill_tools(messages)
        if skill and not resume_messages:
            await self._preapply_skill(skill, messages, emit, request_id)
        self._vision = llm.supports_vision(provider, model, extra)
        self._image_messages: list[tuple[int, int, str]] = []  # (message index, step, paths)
        self._call_nudges = 0
        self._empty_nudges = 0
        self.perf = {"model_s": 0.0, "tool_s": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
        run_t0 = time.time()
        for step in range(1, self.max_steps + 1):
            self._step = step
            self._drop_images(messages, step=step)
            t0 = time.time()

            async def progress(chars: int, thinking: int = 0, _step=step, _t0=t0) -> None:
                key = "loop.generating" if chars else "loop.thinking"
                await emit(AgentEvent(request_id=request_id, type=EventType.TOOL_START, text="", meta={
                    "status": t(key, step=_step, total=self.max_steps, chars=chars or thinking,
                                seconds=int(time.time() - _t0))}))

            try:
                turn = await llm.complete_with_tools(
                    provider=provider, model=model, messages=messages, tools=offered(),
                    api_key=self.api_key, api_base=api_base, extra=extra,
                    semantic_profile=semantic_profile, on_progress=progress,
                )
            except Exception:
                if not self._image_messages:
                    raise
                # The endpoint refused the images: carry on without them for this run.
                self._vision = False
                self._drop_images(messages, everything=True)
                turn = await llm.complete_with_tools(
                    provider=provider, model=model, messages=messages, tools=offered(),
                    api_key=self.api_key, api_base=api_base, extra=extra,
                    semantic_profile=semantic_profile,
                )
            self._record_call(step, time.time() - t0, getattr(turn, "usage", None) or {})
            if getattr(turn, "managed_messages", None) is not None:
                # Keep the raw transcript as the session source of truth.  The
                # compacted view is only what was sent to the model and must
                # not overwrite tool results or source text needed later.
                self.last_managed_messages = list(turn.managed_messages)
            if not turn.tool_calls and turn.content and text_tool_calls.looks_like_call(turn.content):
                # The model wrote its tool call as text. Run it if it can be read;
                # otherwise ask once or twice for a real call instead of ending here.
                recovered = text_tool_calls.recover(turn.content, offered())
                audit = getattr(self.ctx, "audit", None)
                if recovered is not None:
                    turn.content, turn.tool_calls = recovered
                    if audit is not None and hasattr(audit, "_write"):
                        audit._write({"kind": "text_tool_call_recovered", "step": step,
                                      "tools": [c.name for c in turn.tool_calls]})
                elif self._call_nudges < TEXT_CALL_NUDGES:
                    self._call_nudges += 1
                    messages.append({"role": "assistant", "content": turn.content})
                    messages.append({"role": "user", "content": t("loop.text_tool_call")})
                    if audit is not None and hasattr(audit, "_write"):
                        audit._write({"kind": "text_tool_call_nudged", "step": step})
                    continue
            if not turn.tool_calls and not (turn.content or "").strip() and self._empty_nudges < EMPTY_ANSWER_NUDGES:
                # Nothing said and nothing done (often right after a failed step): not finished.
                self._empty_nudges += 1
                messages.append({"role": "user", "content": t("loop.empty_answer")})
                audit = getattr(self.ctx, "audit", None)
                if audit is not None and hasattr(audit, "_write"):
                    audit._write({"kind": "empty_answer_nudged", "step": step})
                continue
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
            images: list[str] = []
            for tc in turn.tool_calls:
                result_text = await self._run_tool(tc, emit, request_id)
                if self._last_images and not self._vision:
                    result_text += "\n" + t("loop.image_unseen")
                images.extend(self._last_images)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

            if images and self._vision:
                self._attach_images(messages, images, step)

        # Expose the run's end state so the orchestrator can offer "继续":
        # on limit it stores the transcript and resumes from it next request.
        self.reached_limit = reached_limit
        self._drop_images(messages, everything=True)  # no base64 renders in the saved transcript
        self.final_messages = list(messages)

        if reached_limit:
            note = t("core.max_steps")
            final_text += note
            await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN, text=note))

        self.perf["total_s"] = time.time() - run_t0
        if audit is not None and hasattr(audit, "_write"):
            audit._write({"kind": "run_perf", **{k: round(v, 1) if isinstance(v, float) else v
                                                 for k, v in self.perf.items()}})
        if self.perf["calls"] > 1:
            await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN, text=t(
                "loop.perf", total=_seconds(self.perf["total_s"]), model=_seconds(self.perf["model_s"]),
                tools=_seconds(self.perf["tool_s"]), calls=self.perf["calls"],
                tokens_in=_thousands(self.perf["input_tokens"]), tokens_out=_thousands(self.perf["output_tokens"]))))

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

    def _record_call(self, step: int, seconds: float, usage: dict) -> None:
        tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        self.perf["model_s"] += seconds
        self.perf["calls"] += 1
        self.perf["input_tokens"] += tokens_in
        self.perf["output_tokens"] += tokens_out
        audit = getattr(self.ctx, "audit", None)
        if audit is not None and hasattr(audit, "_write"):
            audit._write({"kind": "llm_call", "step": step, "ms": int(seconds * 1000),
                          "input_tokens": tokens_in, "output_tokens": tokens_out})

    async def _preapply_skill(self, skill: str, messages: list, emit: Emit, request_id: str) -> None:
        """Start the run with the skill triage picked already applied: its instructions in the
        transcript and its tools unlocked, instead of hoping the model asks for it."""
        args = {"name": skill}
        result = await self.registry.dispatch("apply_skill", args, self.ctx)
        audit = getattr(self.ctx, "audit", None)
        if audit is not None and hasattr(audit, "_write"):
            audit._write({"kind": "skill_preapplied", "skill": skill, "ok": bool(result.ok)})
        if not result.ok:
            return
        call_id = "preapplied_skill"
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "apply_skill", "arguments": json.dumps(args, ensure_ascii=False)}}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": result.text})
        await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN, text=t("loop.skill_preapplied", name=skill)))

    def _reactivate_skill_tools(self, messages: list) -> None:
        """A resumed run ("继续") keeps the tools its skills already unlocked."""
        active = getattr(self.ctx, "active_tools", None)
        if not isinstance(active, set):
            return
        skill_tool = getattr(self.registry, "_tools", {}).get("apply_skill")
        skills = getattr(skill_tool, "_skills", None) or {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                active.add(fn.get("name") or "")
                if fn.get("name") == "apply_skill":
                    try:
                        skill = skills.get(json.loads(fn.get("arguments") or "{}").get("name"))
                    except ValueError:
                        skill = None
                    if skill is not None:
                        from pillow_assistant.core.tools.builtin.skill_tool import skill_tools
                        active.update(skill_tools(skill))

    def _attach_images(self, messages: list, paths: list[str], step: int) -> None:
        content: list[dict[str, Any]] = []
        shown = []
        for path in paths:
            try:
                if len(shown) >= IMAGES_PER_STEP or os.path.getsize(path) > IMAGE_MAX_BYTES:
                    continue
                content.append({"type": "image_url", "image_url": {"url": llm.encode_image_data_url(path)}})
            except OSError:
                continue
            shown.append(path)
        if not shown:
            return
        names = ", ".join(os.path.basename(p) for p in shown)
        messages.append({"role": "user", "content": [{"type": "text", "text": t("loop.tool_images", names=names)},
                                                     *content]})
        self._image_messages.append((len(messages) - 1, step, names))

    def _drop_images(self, messages: list, step: int = 0, everything: bool = False) -> None:
        keep = []
        for index, shown_at, names in self._image_messages:
            if (everything or step - shown_at > IMAGE_KEEP_STEPS) and index < len(messages):
                messages[index] = {"role": "user", "content": t("loop.image_dropped", names=names)}
            else:
                keep.append((index, shown_at, names))
        self._image_messages = keep

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
        self._last_images = list(getattr(result, "images", None) or [])
        if hasattr(self, "perf"):
            self.perf["tool_s"] += time.time() - t0
        summary = result.text
        self.tool_evidence.append({
            "tool_call_id": getattr(tc, "id", None),
            "tool_name": tc.name,
            "ok": bool(getattr(result, "ok", False)),
            "text": summary,
            "artifacts": list(getattr(result, "artifacts", None) or []),
        })
        quiet = tc.name in QUIET_TOOLS
        shown = _quiet_line(tc.name, args, bool(getattr(result, "ok", False))) if quiet else summary
        await emit(AgentEvent(
            request_id=request_id,
            type=EventType.TOOL_RESULT,
            text=shown,
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
        brief = shown if quiet else (summary if len(summary) <= 800 else summary[:800] + "…")
        await emit(AgentEvent(request_id=request_id, type=EventType.TOKEN, text=brief + "\n"))
        return summary
