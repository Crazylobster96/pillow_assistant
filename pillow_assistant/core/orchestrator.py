"""Bus handler: triage (chat / continue / new), route a model, run the Agent
with a pluggable tool registry, and audit-log the run (R1++ / T0–T3).
"""

from __future__ import annotations

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
from pillow_assistant.core.triage import triage

Emit = Callable[[AgentEvent], Awaitable[None]]
CHAT_MEMORY_TURNS = 12


class Orchestrator:
    def __init__(self, storage: Any, vault: Any, project_manager: Any, max_steps: int = 6,
                 undo_manager: Any = None, ask_broker: Any = None) -> None:
        self.storage = storage
        self.vault = vault
        self.pm = project_manager
        self.max_steps = max_steps
        self.undo_manager = undo_manager
        self.ask_broker = ask_broker
        self.registry = build_default_registry()
        self._chat_history: list[dict] = self._load_chat_history()
        self._mcp_loaded = False
        self._register_skills()

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

    def _register_skills(self) -> None:
        try:
            from pillow_assistant.core.skills import SkillStore
            from pillow_assistant.core.tools.builtin.skill_tool import SkillTool

            skills = SkillStore(Path.home() / ".pillow" / "skills").load()
            if skills:
                self.registry.register(SkillTool(skills))
        except Exception:
            pass

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

    def _agent(self, cfg, api_key, workspace, emit, refs, audit, request_id=""):
        ask = None
        if self.ask_broker is not None:
            async def ask(spec, _b=self.ask_broker, _e=emit, _rid=request_id):
                return await _b.ask(_e, _rid, spec)
        ctx = ToolContext(workspace=Path(workspace), session=self.pm.session, emit=emit,
                          vault=self.vault, references=list(refs or []), audit=audit,
                          undo_manager=self.undo_manager, request_id=request_id,
                          storage=self.storage, ask=ask)
        return ToolLoopAgent(cfg=cfg, api_key=api_key, registry=self.registry, ctx=ctx,
                             max_steps=self.max_steps)

    async def __call__(self, request: AppRequest, emit: Emit) -> None:
        configs = self.storage.list_model_configs()
        context_text, ref_images = references.materialize(request.references)
        image_paths = ([request.image_path] if request.image_path else []) + ref_images

        # Multi-model routing: prefer a vlm model when images are involved;
        # honor agent/user-assigned purpose roles (chat / vision).
        from pillow_assistant.core.model_roles import load_roles
        ref = select_model(configs, request.model_ref, want_vision=bool(image_paths),
                           roles=load_roles())
        cfg = self.storage.get_model_config(ref)
        if cfg is None:
            await emit(AgentEvent(request_id=request.id, type=EventType.ERROR, text=t("core.no_model")))
            return
        api_key = self.vault.get_secret(cfg["display_name"]) if self.vault else None

        await self._ensure_mcp()

        index = self.pm.store.index()
        current_id = getattr(self.pm.session, "project_id", None) if self.pm.session else None
        tr = await triage(request.prompt, index, cfg=cfg, api_key=api_key, current_id=current_id)

        await emit(AgentEvent(request_id=request.id, type=EventType.START))

        if tr.is_chat:
            await self._run_chat(request, emit, cfg, api_key, context_text, image_paths)
            return

        project = self.pm.apply(tr, request.prompt)
        session_id = getattr(self.pm.session, "session_id", None)
        note = t("core.project_note", name=project.name) + (
            t("core.project_continue") if tr.action == "continue" else t("core.project_new"))
        await emit(AgentEvent(request_id=request.id, type=EventType.TOKEN, text=note + "\n"))

        audit = AuditLog(project.root / "audit.jsonl")
        audit.run_start(request.prompt)
        history = self.pm.store.load_history(project, session_id)
        agent = self._agent(cfg, api_key, project.workspace, emit, request.references, audit,
                            request_id=request.id)
        final_text = await agent.run(
            prompt=request.prompt, emit=emit, request_id=request.id,
            context=context_text, image_paths=image_paths or None, history=history,
        )
        audit.run_end(len(final_text))
        self.pm.store.record_turn(project, session_id, request.prompt, final_text)

    async def _run_chat(self, request, emit, cfg, api_key, context_text, image_paths) -> None:
        await emit(AgentEvent(request_id=request.id, type=EventType.TOKEN, text=t("core.chat_note") + "\n"))
        chat_dir = Path.home() / ".pillow" / "chat"
        (chat_dir / "workspace").mkdir(parents=True, exist_ok=True)
        audit = AuditLog(chat_dir / "audit.jsonl")
        audit.run_start(request.prompt)
        agent = self._agent(cfg, api_key, chat_dir / "workspace", emit, request.references, audit,
                            request_id=request.id)
        final_text = await agent.run(
            prompt=request.prompt, emit=emit, request_id=request.id,
            context=context_text, image_paths=image_paths or None,
            history=list(self._chat_history),
        )
        audit.run_end(len(final_text))
        self._chat_history.append({"role": "user", "content": request.prompt})
        self._chat_history.append({"role": "assistant", "content": final_text})
        self._chat_history = self._chat_history[-CHAT_MEMORY_TURNS:]
        self._append_chat_history(request.prompt, final_text)
