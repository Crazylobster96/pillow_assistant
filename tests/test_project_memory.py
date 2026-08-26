from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pillow_assistant.contracts import AppRequest, EventType
from pillow_assistant.core import context_budget, llm, semantic_context
from pillow_assistant.core.project_manager import ProjectManager
from pillow_assistant.core.project_memory import (
    ProjectMemoryContext,
    ProjectMemoryService,
    parse_project_turn_delta,
    render_project_memory_context,
)
from pillow_assistant.core.session import Session
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.project_memory_tools import RequestProjectMemoryTool
from storage.db import Storage
from storage.project_memory import (
    InvalidTransition,
    ProjectMemoryError,
    ProjectMemoryStore,
    RevisionConflict,
    ValidationPlanError,
)
from storage.projects import ProjectStore


def required_check(check_id: str = "check-1", check_type: str = "model_review") -> list[dict]:
    return [{
        "id": check_id,
        "title": "required validation",
        "type": check_type,
        "required": True,
    }]


class ProjectMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = ProjectStore(self.root / "projects")
        self.project = self.projects.create("memory-test")
        self.store = ProjectMemoryStore(self.root / "assistant.db", self.projects.base)
        self.store.ensure_schema()
        self.store.ensure_project(self.project.id, "ship safely")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _implementation_complete(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        assert task is not None
        task = self.store.update_task(task_id, base_revision=task["revision"], status="in_progress")
        return self.store.update_task(
            task_id, base_revision=task["revision"], status="implementation_complete"
        )

    def _pass(self, task_id: str, check_id: str) -> dict:
        task = self.store.get_task(task_id)
        assert task is not None
        self.store.record_validation_result(
            task_id, check_id, status="passed", evidence_type="model_review",
            summary="review passed", task_revision=task["revision"],
        )
        return self.store.evaluate_task_completion(task_id)

    def test_schema_and_project_initialization_are_idempotent(self) -> None:
        first = self.store.get_state(self.project.id)
        self.store.ensure_schema()
        second = self.store.ensure_project(self.project.id, "must not overwrite")
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual("ship safely", second["project_goal"])
        with closing(self.store.connect()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'project_memory_%'"
            ).fetchone()[0]
        self.assertEqual(11, count)

    def test_project_state_compare_and_set_rejects_stale_revision(self) -> None:
        state = self.store.get_state(self.project.id)
        updated = self.store.update_state(
            self.project.id, base_revision=state["revision"], state_summary="one"
        )
        self.assertEqual(state["revision"] + 1, updated["revision"])
        with self.assertRaises(RevisionConflict):
            self.store.update_state(
                self.project.id, base_revision=state["revision"], state_summary="lost update"
            )

    def test_empty_validation_plan_and_direct_done_are_rejected(self) -> None:
        with self.assertRaises(ValidationPlanError):
            self.store.create_task(self.project.id, "bad", validation_checks=[])
        task = self.store.create_task(
            self.project.id, "safe", validation_checks=required_check()
        )
        with self.assertRaises(InvalidTransition):
            self.store.update_task(task["id"], base_revision=task["revision"], status="done")

    def test_completion_gate_requires_current_valid_evidence(self) -> None:
        task = self.store.create_task(
            self.project.id, "complete", validation_checks=required_check()
        )
        task = self._implementation_complete(task["id"])
        task = self._pass(task["id"], "check-1")
        self.assertEqual("done", task["status"])
        self.assertIsNotNone(task["completed_at"])

    def test_failed_and_user_waiting_validation_states(self) -> None:
        for result, expected in (("failed", "validation_failed"),
                                 ("awaiting_user", "awaiting_user_validation")):
            check_id = f"check-{result}"
            task = self.store.create_task(
                self.project.id, result, validation_checks=required_check(check_id)
            )
            task = self._implementation_complete(task["id"])
            self.store.record_validation_result(
                task["id"], check_id, status=result, evidence_type="manual",
                summary=result, task_revision=task["revision"],
            )
            self.assertEqual(expected, self.store.evaluate_task_completion(task["id"])["status"])

    def test_open_child_prevents_parent_completion(self) -> None:
        parent = self.store.create_task(
            self.project.id, "parent", validation_checks=required_check("parent-check")
        )
        self.store.create_task(
            self.project.id, "child", parent_task_id=parent["id"],
            validation_checks=required_check("child-check"),
        )
        parent = self._implementation_complete(parent["id"])
        self.store.record_validation_result(
            parent["id"], "parent-check", status="passed", evidence_type="model_review",
            summary="passed", task_revision=parent["revision"],
        )
        self.assertEqual("validating", self.store.evaluate_task_completion(parent["id"])["status"])

    def test_task_and_step_changes_preserve_but_stale_old_evidence(self) -> None:
        task = self.store.create_task(
            self.project.id, "mutable", validation_checks=required_check("old-check")
        )
        done = self._pass(self._implementation_complete(task["id"])["id"], "old-check")
        changed = self.store.update_task(
            done["id"], base_revision=done["revision"], description="new acceptance scope"
        )
        self.assertEqual("needs_review", changed["status"])
        self.assertEqual(done["revision"] + 1, changed["revision"])
        current_checks = self.store.list_checks(done["id"])
        self.assertEqual(1, len(current_checks))
        self.assertEqual("pending", current_checks[0]["status"])
        self.assertNotEqual("old-check", current_checks[0]["id"])
        with closing(self.store.connect()) as connection:
            old_check = connection.execute(
                "SELECT status, task_revision FROM project_memory_checks WHERE id='old-check'"
            ).fetchone()
            evidence = connection.execute(
                "SELECT valid FROM project_memory_evidence WHERE check_id='old-check'"
            ).fetchone()
        self.assertEqual("stale", old_check["status"])
        self.assertEqual(0, evidence["valid"])

        step = self.store.add_step(done["id"], "implement")
        revision = self.store.get_task(done["id"])["revision"]
        self.store.update_step(step["id"], status="done", result_summary="implemented")
        after = self.store.get_task(done["id"])
        self.assertEqual(revision + 1, after["revision"])
        self.assertEqual(1, after["progress"]["completed_steps"])

    def test_replace_plan_keeps_old_evidence_for_audit(self) -> None:
        task = self.store.create_task(
            self.project.id, "replace", validation_checks=required_check("stable-id")
        )
        task = self._implementation_complete(task["id"])
        self.store.record_validation_result(
            task["id"], "stable-id", status="passed", evidence_type="model_review",
            summary="old", task_revision=task["revision"],
        )
        checks = self.store.replace_validation_plan(
            task["id"], base_revision=task["revision"], checks=required_check("stable-id")
        )
        self.assertEqual(1, len(checks))
        self.assertNotEqual("stable-id", checks[0]["id"])
        with closing(self.store.connect()) as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM project_memory_checks WHERE task_id=?", (task["id"],)
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM project_memory_evidence WHERE task_id=?", (task["id"],)
            ).fetchone()[0])

    def test_turn_memory_is_content_idempotent(self) -> None:
        kwargs = dict(
            base_revision=1, new_revision=2, user_summary="u", assistant_summary="a",
            delta={"schema_version": 1}, checkpoint_summary="checkpoint",
        )
        first = self.store.append_turn_memory(self.project.id, "s1", "turn-1", **kwargs)
        second = self.store.append_turn_memory(self.project.id, "s1", "turn-1", **kwargs)
        self.assertEqual(first["turn_id"], second["turn_id"])
        with self.assertRaises(ProjectMemoryError):
            self.store.append_turn_memory(
                self.project.id, "s1", "turn-1", **{**kwargs, "assistant_summary": "different"}
            )

    def test_search_memory_filters_kind_and_task(self) -> None:
        task = self.store.create_task(
            self.project.id, "search", validation_checks=required_check()
        )
        self.store.add_memory_item(
            self.project.id, "decision", "Use SQLite as authoritative storage",
            task_id=task["id"], confidence=0.9,
        )
        self.store.add_memory_item(self.project.id, "fact", "unrelated weather", confidence=0.9)
        hits = self.store.search_memory(
            self.project.id, "SQLite storage", kinds=["decision"], task_id=task["id"]
        )
        self.assertEqual(1, len(hits))
        self.assertEqual("decision", hits[0]["kind"])

    def test_required_miss_creates_pending_request(self) -> None:
        service = ProjectMemoryService(self.store)
        result = service.request_memory(
            self.project.id, "information that does not exist", required=True
        )
        self.assertEqual([], result["hits"])
        self.assertEqual("pending", result["request"]["status"])

    def test_external_source_is_not_copied_and_change_invalidates_revision(self) -> None:
        source_file = self.root / "external.txt"
        source_file.write_text("version one", encoding="utf-8")
        source = self.store.register_source(self.project.id, source_file)
        self.assertEqual(os.path.normcase(str(source_file.resolve())), source["normalized_path"])
        self.assertFalse((self.project.root / "memory" / source_file.name).exists())
        task = self.store.create_task(
            self.project.id, "source-task", validation_checks=required_check("source-check")
        )
        task = self._implementation_complete(task["id"])
        self.store.record_validation_result(
            task["id"], "source-check", status="passed", evidence_type="model_review",
            summary="source checked", task_revision=task["revision"], source_id=source["id"],
        )
        done = self.store.evaluate_task_completion(task["id"])
        source_file.write_text("version two is longer", encoding="utf-8")
        refreshed = self.store.refresh_source(source["id"])
        changed = self.store.get_task(task["id"])
        self.assertEqual("changed", refreshed["availability"])
        self.assertEqual(done["revision"] + 1, changed["revision"])
        self.assertEqual("needs_review", changed["status"])
        self.assertEqual("pending", self.store.list_checks(task["id"])[0]["status"])

    def test_resume_round_trip_and_cleanup(self) -> None:
        messages = [{"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "raw result"}]
        self.store.save_resume(self.project.id, "session", messages, "fingerprint")
        self.assertEqual(messages, self.store.load_resume(self.project.id, "session")["messages"])
        self.store.clear_resume(self.project.id, "session")
        self.assertIsNone(self.store.load_resume(self.project.id, "session"))

    def test_outbox_flush_and_project_delete(self) -> None:
        self.store.add_memory_item(self.project.id, "fact", "durable")
        event_file = self.project.root / "memory" / "events.jsonl"
        ids = [json.loads(line)["event_id"] for line in event_file.read_text("utf-8").splitlines()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(0, self.store.flush_events(self.project.id))
        self.store.delete_project_memory(self.project.id)
        self.assertIsNone(self.store.get_state(self.project.id))
        with closing(self.store.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM project_memory_items WHERE project_id=?", (self.project.id,)
            ).fetchone()[0])


class ProjectMemoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.projects = ProjectStore(root / "projects")
        self.project = self.projects.create("service-test")
        self.store = ProjectMemoryStore(root / "assistant.db", self.projects.base)
        self.store.ensure_schema()
        self.service = ProjectMemoryService(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_delta_parser_and_invalid_delta_fallback(self) -> None:
        parsed = parse_project_turn_delta(
            '```json\n{"schema_version":1,"state_summary":"ok","unknown":1}\n```'
        )
        self.assertEqual({"schema_version": 1, "state_summary": "ok"}, parsed)
        self.assertIsNone(parse_project_turn_delta({"schema_version": 2}))
        result = asyncio.run(self.service.record_turn(
            self.project.id, "s", "fallback-turn", "request", "answer",
            cfg={}, api_key=None, delta={"schema_version": 2},
        ))
        self.assertEqual("fallback", result["status"])
        self.assertTrue(result["state"]["needs_reconcile"])

    def test_service_creates_task_with_default_validation(self) -> None:
        result = asyncio.run(self.service.record_turn(
            self.project.id, "s", "create-turn", "build feature", "started",
            cfg={}, api_key=None,
            delta={
                "schema_version": 1,
                "state_summary": "feature planned",
                "current_task_id": "task-service",
                "tasks_to_create": [{"id": "task-service", "title": "Build feature"}],
                "next_actions": ["implement"],
            },
        ))
        self.assertEqual("applied", result["status"])
        self.assertEqual("task-service", result["state"]["current_task_id"])
        checks = self.store.list_checks("task-service")
        self.assertEqual(1, len(checks))
        self.assertTrue(checks[0]["required"])

    def test_fake_tool_evidence_cannot_pass_validation(self) -> None:
        task = self.store.create_task(
            self.project.id, "validate", validation_checks=required_check("tool-check", "command")
        )
        task = self.store.update_task(task["id"], base_revision=1, status="in_progress")
        task = self.store.update_task(task["id"], base_revision=1, status="implementation_complete")
        delta = {
            "schema_version": 1,
            "validation_results": [{
                "task_id": task["id"], "check_id": "tool-check",
                "task_revision": task["revision"], "status": "passed",
                "evidence_type": "tool", "tool_call_id": "invented", "summary": "passed",
            }],
        }
        rejected = asyncio.run(self.service.record_turn(
            self.project.id, "s", "reject-turn", "validate", "done",
            cfg={}, api_key=None, delta=delta, tool_evidence=[],
        ))
        self.assertTrue(any("missing or failed" in error for error in rejected["errors"]))
        self.assertEqual("pending", self.store.list_checks(task["id"])[0]["status"])

        accepted = asyncio.run(self.service.record_turn(
            self.project.id, "s", "accept-turn", "validate", "done",
            cfg={}, api_key=None, delta=delta,
            tool_evidence=[{
                "tool_call_id": "invented", "tool_name": "run_cli", "ok": True,
                "text": "tests passed", "artifacts": [],
            }],
        ))
        self.assertFalse(accepted["errors"])
        self.assertEqual("done", self.store.get_task(task["id"])["status"])

    def test_rendered_state_survives_deterministic_and_semantic_compaction(self) -> None:
        state = self.store.ensure_project(self.project.id)
        ctx = ProjectMemoryContext(
            state={**state, "blockers": ["critical blocker"], "state_summary": "authoritative"},
            relevant_items=[{"kind": "fact", "source_id": "x", "content": "old " * 5000}],
        )
        rendered = render_project_memory_context(ctx, max_chars=2500)
        self.assertLessEqual(len(rendered), 2500)
        huge_task = {
            "id": "task-huge", "title": "large task", "description": "detail " * 5000,
            "status": "validating", "revision": 77, "current_step_id": "step-99",
            "blockers": ["critical blocker"],
            "progress": {
                "completed_steps": 99, "total_steps": 100,
                "passed_required_checks": 0, "total_required_checks": 100,
            },
            "steps": [{
                "id": f"step-{index}", "ordinal": index, "title": "step " + "x" * 300,
                "status": "done" if index < 99 else "in_progress", "result_summary": "r" * 500,
            } for index in range(100)],
            "validation_checks": [{
                "id": f"check-{index}", "title": "check " + "y" * 300,
                "check_type": "command", "required": True, "status": "pending",
                "task_revision": 77, "config": {"command": "z" * 1000},
            } for index in range(100)],
        }
        bounded = render_project_memory_context(ProjectMemoryContext(
            state={**state, "current_task_id": "task-huge", "blockers": ["critical blocker"]},
            current_task=huge_task, relevant_items=ctx.relevant_items,
        ), max_chars=2500)
        self.assertLessEqual(len(bounded), 2500)
        self.assertIn("task-huge", bounded)
        self.assertIn(context_budget.PROJECT_STATE_CLOSE, bounded)
        wrapped = context_budget.join_context_and_prompt(rendered, "current request")
        compacted = context_budget._compact_marked_context(wrapped, 0.2)
        self.assertIn(context_budget.PROJECT_STATE_OPEN, compacted)
        self.assertIn("critical blocker", compacted)
        source, replaced = semantic_context._remove_marked_text(wrapped)
        self.assertIn(context_budget.PROJECT_STATE_OPEN, replaced)
        self.assertIn("critical blocker", replaced)
        self.assertNotIn(context_budget.PROJECT_STATE_OPEN, source)

    def test_memory_tool_enforces_two_retrieval_limit(self) -> None:
        self.store.ensure_project(self.project.id)
        self.store.add_memory_item(self.project.id, "fact", "SQLite stores state")
        ctx = ToolContext(
            workspace=self.project.workspace, project_memory=self.service,
            project_id=self.project.id, request_id="turn",
        )
        tool = RequestProjectMemoryTool()
        first = asyncio.run(tool({"query": "SQLite"}, ctx))
        second = asyncio.run(tool({"query": "state"}, ctx))
        third = asyncio.run(tool({"query": "again"}, ctx))
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(third.ok)


class ProjectMemoryOrchestratorTests(unittest.TestCase):
    def test_project_context_is_injected_and_turn_is_written_back(self) -> None:
        import pillow_assistant.core.orchestrator as orchestrator_module
        from pillow_assistant.core.orchestrator import Orchestrator
        from pillow_assistant.core.triage import TriageResult

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "assistant.db")
            storage.ensure_schema()
            storage.replace_model_configs([{
                "provider": "OpenAI", "model_type": "llm", "display_name": "m", "model": "x",
            }])
            project_store = ProjectStore(root / "projects")
            manager = ProjectManager(project_store, Session())
            captured: dict = {}
            events = []
            original_triage = orchestrator_module.triage
            original_complete = llm.complete
            original_tools = llm.complete_with_tools

            async def fake_triage(prompt, index, *, cfg, api_key, current_id=None):
                return TriageResult(action="new", confidence=1.0, rationale="test")

            async def fake_complete(**kwargs):
                return json.dumps({
                    "schema_version": 1,
                    "state_summary": "turn recorded",
                    "current_task_id": "task-orchestrator",
                    "tasks_to_create": [{
                        "id": "task-orchestrator", "title": "Orchestrated task",
                        "validation_checks": required_check("orch-check"),
                    }],
                    "next_actions": ["validate"],
                })

            async def fake_tools(**kwargs):
                captured["user_content"] = kwargs["messages"][-1]["content"]
                return llm.ToolTurn(content="agent answer", tool_calls=[])

            async def emit(event):
                events.append(event)

            try:
                orchestrator_module.triage = fake_triage
                llm.complete = fake_complete
                llm.complete_with_tools = fake_tools
                asyncio.run(Orchestrator(storage, None, manager)(
                    AppRequest(prompt="build it", model_ref="m"), emit
                ))
            finally:
                orchestrator_module.triage = original_triage
                llm.complete = original_complete
                llm.complete_with_tools = original_tools

            self.assertIn(context_budget.PROJECT_STATE_OPEN, captured["user_content"])
            project = project_store.list()[0]
            memory = ProjectMemoryStore(storage.db_path, project_store.base)
            self.assertEqual("task-orchestrator", memory.get_state(project.id)["current_task_id"])
            self.assertIsNotNone(memory.get_last_turn_memory(project.id))
            self.assertEqual(EventType.DONE, events[-1].type)


if __name__ == "__main__":
    unittest.main()
