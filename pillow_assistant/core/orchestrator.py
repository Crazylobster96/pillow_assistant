"""Bus handler: triage (chat / continue / new), route a model, run the Agent
with a pluggable tool registry, and audit-log the run (R1++ / T0–T3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType
from pillow_assistant.core import references
from pillow_assistant.core.agent.loop import ToolLoopAgent
from pillow_assistant.core.i18n import t
from pillow_assistant.core.model_router import select_model
from pillow_assistant.core.observability import AuditLog
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin import build_default_registry
from pillow_assistant.core.triage import TriageResult, triage

Emit = Callable[[AgentEvent], Awaitable[None]]
CHAT_MEMORY_TURNS = 12
# Auto-switch the conversation into a *different* past project only when triage
# is confident enough; below this it keeps chatting (no switch). 0.8 balances
# responsiveness against mis-switches (LLM confidence clusters at 0.7/0.8/0.9).
SWITCH_CONFIDENCE = 0.8
# Switching *away* from a project you're actively working in is riskier (it can
# orphan the thread), so it needs a higher bar than attaching from one-off chat.
SWITCH_AWAY_CONFIDENCE = 0.9


def _is_resume_request(prompt: str) -> bool:
    text = " ".join(str(prompt or "").strip().lower().split()).strip("。.!！?？")
    if text in {"继续", "继续执行", "接着做", "恢复", "恢复任务", "resume", "continue"}:
        return True
    return text.startswith(("继续上次", "继续刚才", "resume ", "continue "))


class Orchestrator:
    def __init__(self, storage: Any, vault: Any, project_manager: Any, max_steps: int = 50,
                 undo_manager: Any = None, ask_broker: Any = None) -> None:
        self.storage = storage
        self.vault = vault
        self.pm = project_manager
        self.max_steps = max_steps
        self.undo_manager = undo_manager
        self.ask_broker = ask_broker
        self.registry = build_default_registry()
        self._chat_history: list[dict] = self._load_chat_history()
        # Paused transcripts of runs that hit the step limit, keyed by
        # (project_id, session_id) or "chat" — replying「继续」resumes them
        # with the full tool-call context (in-memory, cleared on use).
        self._resume_state: dict = {}
        self._mcp_loaded = False
        self.conversation_memory = None
        try:
            from pillow_assistant.core.conversation_memory import ConversationMemoryService
            from storage.conversation import ConversationMemoryStore

            db_path = getattr(storage, "db_path", None)
            if db_path is not None:
                store = ConversationMemoryStore(db_path)
                store.ensure_schema()
                self.conversation_memory = ConversationMemoryService(store)
        except Exception:
            self.conversation_memory = None
        self.project_memory = None
        self.project_memory_backend_error = None
        try:
            from pillow_assistant.core.project_memory import ProjectMemoryService
            from pillow_assistant.core.rag.level3_backend import build_level3_backend
            from pillow_assistant.core.rag.project_backend import build_layer2_backend
            from pillow_assistant.core.settings import load_settings
            from storage.project_memory import ProjectMemoryStore

            db_path = getattr(storage, "db_path", None)
            projects_base = getattr(getattr(project_manager, "store", None), "base", None)
            if db_path is not None and projects_base is not None:
                project_store = ProjectMemoryStore(db_path, projects_base)
                project_store.ensure_schema()
                memory_settings = load_settings().get("project_memory") or {}
                level2_settings = memory_settings.get("level2") or {}
                try:
                    backend = build_layer2_backend(project_store, db_path, level2_settings)
                except Exception as exc:
                    # Keep the last known-good mandatory Level-1 backend.
                    self.project_memory_backend_error = str(exc)
                    backend = project_store
                level3_settings = memory_settings.get("level3") or {}
                try:
                    backend = build_level3_backend(backend, db_path, level3_settings)
                except Exception as exc:
                    # Preserve the already validated lower-level backend.
                    self.project_memory_backend_error = str(exc)
                self.project_memory = ProjectMemoryService(backend)
        except Exception:
            self.project_memory = None

    # -- chat history persistence (one-off conversations, not project-bound) --
    @staticmethod
    def _chat_history_path() -> Path:
        return Path.home() / ".pillow" / "chat" / "history.jsonl"

    def _load_chat_history(self) -> list[dict]:
        turns: list[dict] = []
        try:
            for line in self._chat_history_path().read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("role") and obj.get("content") is not None:
                    turns.append({"role": obj["role"], "content": str(obj["content"])})
        except (OSError, ValueError):
            return []
        return turns[-CHAT_MEMORY_TURNS:]

    def _append_chat_history(self, prompt: str, answer: str) -> None:
        try:
            path = self._chat_history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"role": "user", "content": prompt}, ensure_ascii=False) + "\n")
                fh.write(json.dumps({"role": "assistant", "content": answer}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _registry_for(self, project_root: Path | None = None):
        """Build a request-local registry and resolve layered Skills without cross-run leakage."""
        from pillow_assistant.capabilities.skill_registry import CapabilitySkillRegistry
        from pillow_assistant.core.tools.builtin.skill_tool import SkillTool

        registry = self.registry.clone()
        project_skills = project_root / ".pillow" / "skills" if project_root is not None else None
        catalog = CapabilitySkillRegistry(project_root=project_skills)
        skills = catalog.load()
        mode = "project" if project_root is not None else "chat"
        if skills:
            registry.register(SkillTool(skills, available_tools=registry.names(mode)))
        return registry, catalog.snapshot()

    async def _ensure_mcp(self) -> None:
        if self._mcp_loaded:
            return
        self._mcp_loaded = True
        try:
            from pillow_assistant.core.tools.mcp import load_mcp_tools, read_mcp_configs

            configs = read_mcp_configs(Path.home() / ".pillow" / "mcp_servers.json")
            for tool in await load_mcp_tools(configs):
                self.registry.register(tool)
        except Exception:
            pass

    def _agent(self, cfg, api_key, workspace, emit, refs, audit, request_id="", project_id=None, project_root=None):
        ask = None
        if self.ask_broker is not None:
            async def ask(spec, _b=self.ask_broker, _e=emit, _rid=request_id):
                return await _b.ask(_e, _rid, spec)
        registry, skill_snapshot = self._registry_for(Path(project_root) if project_root else None)
        ctx = ToolContext(workspace=Path(workspace), session=self.pm.session, emit=emit,
                          vault=self.vault, references=list(refs or []), audit=audit,
                          undo_manager=self.undo_manager, request_id=request_id,
                          storage=self.storage, ask=ask, project_store=self.pm.store,
                          project_memory=self.project_memory, project_id=project_id,
                          memory_request_count=0, skill_snapshot=skill_snapshot)
        # User-adjustable step budget (set_max_steps tool); read per request.
        try:
            from pillow_assistant.core.settings import load_settings
            steps = int(load_settings().get("max_steps") or self.max_steps)
            steps = max(1, min(steps, 500))
        except Exception:
            steps = self.max_steps
        return ToolLoopAgent(cfg=cfg, api_key=api_key, registry=registry, ctx=ctx,
                             max_steps=steps)

    async def __call__(self, request: AppRequest, emit: Emit) -> None:
        configs = self.storage.list_model_configs() if hasattr(self.storage, "list_model_configs") else []
        context_text, ref_images = references.materialize(request.references)
        image_paths = ([request.image_path] if request.image_path else []) + ref_images

        # Multi-model routing: prefer a vlm model when images are involved;
        # honor agent/user-assigned purpose roles (chat / vision).
        if configs:
            from pillow_assistant.core.model_roles import load_roles
            ref = select_model(configs, request.model_ref, want_vision=bool(image_paths),
                               roles=load_roles())
            cfg = self.storage.get_model_config(ref)
        else:
            cfg = self.storage.get_model_config(request.model_ref)
        if cfg is None:
            await emit(AgentEvent(request_id=request.id, type=EventType.ERROR, text=t("core.no_model")))
            return
        api_key = self.vault.get_secret(cfg["display_name"]) if self.vault else None

        await self._ensure_mcp()

        index = self.pm.store.index()
        current_id = getattr(self.pm.session, "project_id", None) if self.pm.session else None
        tr = await triage(request.prompt, index, cfg=cfg, api_key=api_key, current_id=current_id)

        # A paused run only resumes on an explicit continuation request. Check
        # both the in-memory fast path and the persistent Level-1 transcript.
        persistent_resume = None
        if _is_resume_request(request.prompt) and current_id is not None:
            sid = getattr(self.pm.session, "session_id", None)
            if self.project_memory is not None and sid:
                try:
                    persistent_resume = self.project_memory.load_resume(current_id, sid)
                except Exception:
                    persistent_resume = None
            has_resume = (current_id, sid) in self._resume_state or persistent_resume is not None
            if tr.is_chat and tr.rationale != "app-setting" and has_resume:
                tr = TriageResult(action="continue", project_id=current_id,
                                  confidence=1.0, rationale="resume-pending")

        # Cross-project switch: triage wants to continue a project that is NOT
        # the one the session is currently bound to (or the session was in
        # one-off chat). Only switch automatically once confidence is high
        # (>=SWITCH_CONFIDENCE); below it, keep chatting and don't switch yet —
        # the conversation stays put until a turn is confident enough, then it
        # (and this turn) fold into the project.
        switching = (tr.action == "continue" and tr.project_id
                     and tr.project_id != current_id)
        if switching:
            threshold = SWITCH_AWAY_CONFIDENCE if current_id else SWITCH_CONFIDENCE
            if tr.confidence < threshold:
                if current_id:
                    # Not confident enough to leave the active project — stay in
                    # it and keep its history, rather than drifting away.
                    tr = TriageResult(action="continue", project_id=current_id,
                                      confidence=tr.confidence, rationale="stay-in-current")
                else:
                    # From one-off chat: keep chatting until confident enough.
                    tr = TriageResult(action="chat", confidence=tr.confidence,
                                      rationale="switch-below-threshold")

        await emit(AgentEvent(request_id=request.id, type=EventType.START))

        if tr.is_chat:
            await self._run_chat(request, emit, cfg, api_key, context_text, image_paths)
            return

        project = self.pm.apply(tr, request.prompt)
        session_id = getattr(self.pm.session, "session_id", None)
        if tr.action == "continue" and project.id != current_id:
            note = t("core.project_switch", name=project.name)  # genuine switch
        else:
            note = t("core.project_note", name=project.name) + (
                t("core.project_continue") if tr.action == "continue" else t("core.project_new"))
        await emit(AgentEvent(request_id=request.id, type=EventType.TOKEN, text=note + "\n"))

        audit = AuditLog(project.root / "audit.jsonl")
        audit.run_start(request.prompt)
        # Load the project's recent history ACROSS sessions (session_id=None),
        # not just the current session. A switch / new session must never make
        # the Agent lose the project's accumulated context.
        history = self.pm.store.load_history(project)
        memory_ctx = None
        final_context = context_text
        if self.project_memory is not None:
            try:
                memory_ctx = self.project_memory.prepare_context(
                    project.id, request.prompt, references=request.references
                )
                if memory_ctx.rendered_context:
                    final_context = (
                        (context_text + "\n\n") if context_text else ""
                    ) + memory_ctx.rendered_context
            except Exception:
                memory_ctx = None
        agent = self._agent(cfg, api_key, project.workspace, emit, request.references, audit,
                            request_id=request.id, project_id=project.id, project_root=project.root)
        resume_key = (project.id, session_id)
        resume_messages = None
        if _is_resume_request(request.prompt):
            resume_messages = self._resume_state.pop(resume_key, None)
            if resume_messages is None and persistent_resume is not None:
                resume_messages = persistent_resume.get("messages")
        final_text = await agent.run(
            prompt=request.prompt, emit=emit, request_id=request.id,
            context=final_context, image_paths=image_paths or None, history=history,
            resume_messages=resume_messages,
        )
        hit_limit = bool(getattr(agent, "reached_limit", False))
        if hit_limit:
            self._resume_state[resume_key] = agent.final_messages
            if self.project_memory is not None and session_id:
                try:
                    fingerprint = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
                    self.project_memory.save_resume(
                        project.id, session_id, agent.final_messages, fingerprint
                    )
                except Exception:
                    pass
        elif resume_messages is not None and self.project_memory is not None and session_id:
            try:
                self.project_memory.clear_resume(project.id, session_id)
            except Exception:
                pass
        # Persist whether this project has work left to resume, so the project
        # browser can show which projects are still in progress.
        still_pending = resume_key in self._resume_state
        if self.project_memory is not None and session_id and not still_pending and not hit_limit:
            try:
                still_pending = self.project_memory.load_resume(project.id, session_id) is not None
            except Exception:
                still_pending = False
        self.pm.store.set_unfinished(project, hit_limit or still_pending)
        self.pm.store.record_turn(project, session_id, request.prompt, final_text)
        if self.project_memory is not None and memory_ctx is not None:
            try:
                await self.project_memory.record_turn(
                    project.id, session_id, request.id, request.prompt, final_text,
                    cfg=cfg, api_key=api_key, transcript=agent.final_messages,
                    tool_evidence=getattr(agent, "tool_evidence", []),
                )
            except Exception as exc:
                audit._write({"kind": "project_memory_writeback_failed", "error": str(exc)[:300]})
        if self.project_memory is not None:
            processor = getattr(self.project_memory.store, "process_pending_jobs", None)
            if callable(processor):
                try:
                    # Agent DONE has already been emitted; indexing cannot delay the answer stream.
                    await asyncio.to_thread(processor, project.id, 4)
                except Exception as exc:
                    audit._write({"kind": "project_rag_index_failed", "error": str(exc)[:300]})
        audit.run_end(len(final_text))

    async def _run_chat(self, request, emit, cfg, api_key, context_text, image_paths) -> None:
        await emit(AgentEvent(request_id=request.id, type=EventType.TOKEN, text=t("core.chat_note") + "\n"))
        chat_dir = Path.home() / ".pillow" / "chat"
        (chat_dir / "workspace").mkdir(parents=True, exist_ok=True)
        audit = AuditLog(chat_dir / "audit.jsonl")
        audit.run_start(request.prompt)
        agent = self._agent(cfg, api_key, chat_dir / "workspace", emit, request.references, audit,
                            request_id=request.id)
        memory_ctx = None
        final_context = context_text
        if self.conversation_memory is not None:
            try:
                memory_ctx = await self.conversation_memory.prepare_chat_context(
                    request.prompt, cfg=cfg, api_key=api_key
                )
                if memory_ctx.rendered_context:
                    final_context = (context_text + "\n\n" if context_text else "") + memory_ctx.rendered_context
            except Exception:
                memory_ctx = None
        final_text = await agent.run(
            prompt=request.prompt, emit=emit, request_id=request.id,
            context=final_context, image_paths=image_paths or None,
            history=list(self._chat_history),
            resume_messages=self._resume_state.pop("chat", None),
        )
        if getattr(agent, "reached_limit", False):
            self._resume_state["chat"] = agent.final_messages
        audit.run_end(len(final_text))
        self._chat_history.append({"role": "user", "content": request.prompt})
        self._chat_history.append({"role": "assistant", "content": final_text})
        self._chat_history = self._chat_history[-CHAT_MEMORY_TURNS:]
        self._append_chat_history(request.prompt, final_text)
        if self.conversation_memory is not None and memory_ctx is not None:
            try:
                await self.conversation_memory.record_chat_result(
                    memory_ctx, request.prompt, final_text, cfg=cfg, api_key=api_key
                )
            except Exception:
                pass
