"""Structured, local project memory with validation-gated task completion.

SQLite is the authority for mutable state.  Per-project ``memory/events.jsonl``
files are append-only audit mirrors produced from a SQLite outbox; raw dialogue
continues to live in ``sessions/*.jsonl`` through :mod:`storage.projects`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA_VERSION = 1
TASK_STATUSES = {
    "planned", "in_progress", "blocked", "implementation_complete",
    "validating", "validation_failed", "awaiting_user_validation", "done",
    "needs_review", "cancelled", "superseded",
}
CHECK_TYPES = {
    "command", "artifact_exists", "artifact_content", "requirement_match",
    "state_check", "integration", "regression", "manual", "model_review",
}
CHECK_STATUSES = {"pending", "running", "passed", "failed", "blocked", "awaiting_user", "stale"}
RESULT_STATUSES = {"passed", "failed", "blocked", "awaiting_user"}
MEMORY_KINDS = {
    "requirement", "decision", "fact", "constraint", "failed_attempt",
    "blocker", "artifact", "open_question", "user_correction", "conflict",
}
ALLOWED_TRANSITIONS = {
    "planned": {"in_progress", "blocked", "cancelled", "superseded"},
    "in_progress": {"blocked", "implementation_complete", "cancelled", "superseded"},
    "blocked": {"in_progress", "cancelled", "superseded"},
    "implementation_complete": {"validating", "in_progress", "blocked"},
    "validating": {"validation_failed", "awaiting_user_validation", "in_progress"},
    "validation_failed": {"in_progress", "validating", "cancelled"},
    "awaiting_user_validation": {"validating", "in_progress", "cancelled"},
    "done": {"needs_review"},
    "needs_review": {"in_progress", "validating", "cancelled", "superseded"},
    "cancelled": set(),
    "superseded": set(),
}


class ProjectMemoryError(Exception):
    """Base class for expected project-memory errors."""


class RevisionConflict(ProjectMemoryError):
    """The caller based an update on a stale revision."""


class InvalidTransition(ProjectMemoryError):
    """A task status transition is not allowed."""


class ValidationPlanError(ProjectMemoryError):
    """A task has no usable required validation plan."""


class ValidationEvidenceError(ProjectMemoryError):
    """Validation evidence is missing, stale, or belongs to another task."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json(value: Any, default: Any) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _row(value: sqlite3.Row | None, *, json_fields: Iterable[str] = ()) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    result = dict(value)
    for key in json_fields:
        if key in result:
            result[key.removesuffix("_json")] = _decode(result.pop(key), [])
    return result


def _tokens(text: str) -> set[str]:
    value = (text or "").lower()
    words = set(re.findall(r"[a-zA-Z0-9_./:-]{2,}", value))
    cjk = set(re.findall(r"[\u4e00-\u9fff]{2,}", value))
    bigrams = {
        value[index:index + 2]
        for index in range(max(0, len(value) - 1))
        if any("\u4e00" <= char <= "\u9fff" for char in value[index:index + 2])
    }
    return words | cjk | bigrams


def _clamp_limit(value: Any, default: int = 8, maximum: int = 20) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))


def _normalize_checks(checks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in checks or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        check_type = str(raw.get("type") or raw.get("check_type") or "manual").strip()
        if not title or check_type not in CHECK_TYPES:
            continue
        result.append({
            "id": str(raw.get("id") or _id("check")),
            "title": title,
            "check_type": check_type,
            "required": bool(raw.get("required", True)),
            "config": dict(raw.get("config") or {}) if isinstance(raw.get("config") or {}, dict) else {},
        })
    if not result or not any(item["required"] for item in result):
        raise ValidationPlanError("each task requires at least one required validation check")
    return result


class ProjectMemoryStore:
    def __init__(self, db_path: str | Path, projects_base: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_base = Path(projects_base) if projects_base is not None else Path.home() / ".pillow" / "projects"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def ensure_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS project_memory_state (
                project_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1, project_goal TEXT NOT NULL DEFAULT '',
                project_status TEXT NOT NULL DEFAULT 'active', current_task_id TEXT,
                current_step_id TEXT, state_summary TEXT NOT NULL DEFAULT '',
                blockers_json TEXT NOT NULL DEFAULT '[]', open_questions_json TEXT NOT NULL DEFAULT '[]',
                next_actions_json TEXT NOT NULL DEFAULT '[]', last_turn_id TEXT,
                needs_reconcile INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_tasks (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_task_id TEXT,
                title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
                current_step_id TEXT, blockers_json TEXT NOT NULL DEFAULT '[]',
                created_from_turn_id TEXT, completed_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                FOREIGN KEY(parent_task_id) REFERENCES project_memory_tasks(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_steps (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
                result_summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(task_id, ordinal), FOREIGN KEY(task_id) REFERENCES project_memory_tasks(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_checks (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, title TEXT NOT NULL,
                check_type TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending', task_revision INTEGER NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}', last_evidence_id TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES project_memory_tasks(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_evidence (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL, check_id TEXT NOT NULL,
                task_revision INTEGER NOT NULL, evidence_type TEXT NOT NULL, source_id TEXT,
                tool_call_id TEXT, artifact_path TEXT, artifact_fingerprint TEXT,
                summary TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES project_memory_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(check_id) REFERENCES project_memory_checks(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_turns (
                turn_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT,
                base_revision INTEGER NOT NULL, new_revision INTEGER NOT NULL,
                user_summary TEXT NOT NULL DEFAULT '', assistant_summary TEXT NOT NULL DEFAULT '',
                delta_json TEXT NOT NULL DEFAULT '{}', checkpoint_summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'applied', created_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_items (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                task_id TEXT, source_turn_id TEXT, source_event_id TEXT,
                confidence REAL NOT NULL DEFAULT 0.0, status TEXT NOT NULL DEFAULT 'active',
                supersedes_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_requests (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, origin_turn_id TEXT, query TEXT NOT NULL,
                kinds_json TEXT NOT NULL DEFAULT '[]', task_id TEXT, required INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0, resolved_item_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL, resolved_at REAL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_sources (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, original_path TEXT NOT NULL,
                normalized_path TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'file',
                size INTEGER, mtime_ns INTEGER, content_hash TEXT,
                availability TEXT NOT NULL, source_turn_id TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(project_id, normalized_path)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_resume (
                project_id TEXT NOT NULL, session_id TEXT NOT NULL, messages_json TEXT NOT NULL,
                prompt_fingerprint TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(project_id, session_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_memory_events (
                event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at REAL NOT NULL, mirrored INTEGER NOT NULL DEFAULT 0
            )
            """,
        )
        indexes = (
            "CREATE INDEX IF NOT EXISTS idx_pm_tasks_project_status ON project_memory_tasks(project_id,status,updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pm_tasks_parent ON project_memory_tasks(parent_task_id,status)",
            "CREATE INDEX IF NOT EXISTS idx_pm_checks_task ON project_memory_checks(task_id,required,status)",
            "CREATE INDEX IF NOT EXISTS idx_pm_evidence_task ON project_memory_evidence(task_id,task_revision,valid)",
            "CREATE INDEX IF NOT EXISTS idx_pm_turns_project ON project_memory_turns(project_id,created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pm_items_project ON project_memory_items(project_id,kind,status,updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pm_requests_project ON project_memory_requests(project_id,status,created_at)",
            "CREATE INDEX IF NOT EXISTS idx_pm_events_pending ON project_memory_events(mirrored,project_id,created_at)",
        )
        with closing(self.connect()) as connection:
            for statement in statements:
                connection.execute(statement)
            for statement in indexes:
                connection.execute(statement)
            connection.commit()
        self.flush_events()

    def _queue_event(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        event_id: Optional[str] = None,
    ) -> str:
        identifier = event_id or _id("event")
        connection.execute(
            """
            INSERT OR IGNORE INTO project_memory_events
                (event_id, project_id, kind, payload_json, created_at, mirrored)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (identifier, project_id, kind, _json(payload, {}), time.time()),
        )
        return identifier

    def _event_path(self, project_id: str) -> Optional[Path]:
        root = self.projects_base / project_id
        if not root.is_dir():
            return None
        return root / "memory" / "events.jsonl"

    def flush_events(self, project_id: Optional[str] = None) -> int:
        query = "SELECT * FROM project_memory_events WHERE mirrored = 0"
        params: tuple[Any, ...] = ()
        if project_id:
            query += " AND project_id = ?"
            params = (project_id,)
        query += " ORDER BY created_at, event_id"
        try:
            with closing(self.connect()) as connection:
                rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return 0
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["project_id"]), []).append(row)
        mirrored: list[str] = []
        for pid, events in grouped.items():
            path = self._event_path(pid)
            if path is None:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(_json({
                            "schema_version": SCHEMA_VERSION,
                            "event_id": event["event_id"],
                            "project_id": event["project_id"],
                            "kind": event["kind"],
                            "payload": _decode(event["payload_json"], {}),
                            "created_at": event["created_at"],
                        }, {}) + "\n")
                        mirrored.append(str(event["event_id"]))
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                continue
        if mirrored:
            try:
                with closing(self.connect()) as connection:
                    connection.executemany(
                        "UPDATE project_memory_events SET mirrored = 1 WHERE event_id = ?",
                        [(event_id,) for event_id in mirrored],
                    )
                    connection.commit()
            except sqlite3.Error:
                return 0
        return len(mirrored)

    def ensure_project(self, project_id: str, goal: str = "") -> dict[str, Any]:
        pid = str(project_id or "").strip()
        if not pid:
            raise ProjectMemoryError("project_id is required")
        now = time.time()
        created = False
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO project_memory_state
                    (project_id, schema_version, revision, project_goal, project_status,
                     created_at, updated_at)
                VALUES (?, ?, 1, ?, 'active', ?, ?)
                """,
                (pid, SCHEMA_VERSION, goal or "", now, now),
            )
            created = cursor.rowcount > 0
            if created:
                self._queue_event(connection, pid, "project.created", {"goal": goal or ""})
            connection.commit()
        if created:
            self.flush_events(pid)
        state = self.get_state(pid)
        if state is None:
            raise ProjectMemoryError("failed to initialize project memory")
        return state

    def get_state(self, project_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_state WHERE project_id = ?", (project_id,)
            ).fetchone()
        return _row(row, json_fields=("blockers_json", "open_questions_json", "next_actions_json"))

    def update_state(self, project_id: str, *, base_revision: int, **fields: Any) -> dict[str, Any]:
        mapping = {
            "project_goal": "project_goal", "project_status": "project_status",
            "current_task_id": "current_task_id", "current_step_id": "current_step_id",
            "state_summary": "state_summary", "blockers": "blockers_json",
            "open_questions": "open_questions_json", "next_actions": "next_actions_json",
            "last_turn_id": "last_turn_id", "needs_reconcile": "needs_reconcile",
        }
        assignments: list[str] = []
        values: list[Any] = []
        event_fields: dict[str, Any] = {}
        for key, value in fields.items():
            column = mapping.get(key)
            if column is None:
                continue
            if column.endswith("_json"):
                value = _json(value, [])
            elif column == "needs_reconcile":
                value = 1 if value else 0
            assignments.append(f"{column} = ?")
            values.append(value)
            event_fields[key] = fields[key]
        if not assignments:
            state = self.get_state(project_id)
            if state is None:
                raise ProjectMemoryError("project memory does not exist")
            return state
        assignments.extend(["revision = revision + 1", "updated_at = ?"])
        values.append(time.time())
        values.extend([project_id, int(base_revision)])
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                f"UPDATE project_memory_state SET {', '.join(assignments)} "
                "WHERE project_id = ? AND revision = ?",
                values,
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RevisionConflict(f"project {project_id} revision changed")
            self._queue_event(connection, project_id, "project.state.updated", event_fields)
            connection.commit()
        self.flush_events(project_id)
        state = self.get_state(project_id)
        assert state is not None
        return state

    def create_task(
        self,
        project_id: str,
        title: str,
        *,
        validation_checks: Iterable[dict[str, Any]],
        parent_task_id: Optional[str] = None,
        description: str = "",
        priority: int = 0,
        created_from_turn_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        name = str(title or "").strip()
        if not name:
            raise ProjectMemoryError("task title is required")
        checks = _normalize_checks(validation_checks)
        identifier = task_id or _id("task")
        now = time.time()
        with closing(self.connect()) as connection:
            if parent_task_id:
                parent = connection.execute(
                    "SELECT project_id FROM project_memory_tasks WHERE id = ?", (parent_task_id,)
                ).fetchone()
                if parent is None or parent["project_id"] != project_id:
                    raise ProjectMemoryError("parent task does not belong to project")
            existing = connection.execute(
                "SELECT * FROM project_memory_tasks WHERE id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                if existing["project_id"] == project_id and existing["title"] == name:
                    found = self.get_task(identifier)
                    assert found is not None
                    return found
                raise ProjectMemoryError("task id already exists with different content")
            connection.execute(
                """
                INSERT INTO project_memory_tasks
                    (id, project_id, parent_task_id, title, description, status, priority,
                     revision, blockers_json, created_from_turn_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'planned', ?, 1, '[]', ?, ?, ?)
                """,
                (identifier, project_id, parent_task_id, name, description or "",
                 int(priority or 0), created_from_turn_id, now, now),
            )
            for check in checks:
                connection.execute(
                    """
                    INSERT INTO project_memory_checks
                        (id, task_id, title, check_type, required, status, task_revision,
                         config_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)
                    """,
                    (check["id"], identifier, check["title"], check["check_type"],
                     1 if check["required"] else 0, _json(check["config"], {}), now, now),
                )
            self._queue_event(connection, project_id, "task.created", {
                "task_id": identifier, "title": name, "parent_task_id": parent_task_id,
                "checks": checks,
            })
            connection.commit()
        self.flush_events(project_id)
        task = self.get_task(identifier)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        task = _row(row, json_fields=("blockers_json",))
        if task is not None:
            task["progress"] = self.derive_task_progress(task_id)
        return task

    def list_tasks(
        self,
        project_id: str,
        *,
        statuses: Optional[Iterable[str]] = None,
        parent_task_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if statuses is None:
            clauses.append("status NOT IN ('cancelled','superseded')")
        else:
            valid = [status for status in statuses if status in TASK_STATUSES]
            if not valid:
                return []
            clauses.append(f"status IN ({','.join('?' for _ in valid)})")
            params.extend(valid)
        if parent_task_id is not None:
            clauses.append("parent_task_id = ?")
            params.append(parent_task_id)
        params.append(max(1, min(500, int(limit or 100))))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM project_memory_tasks WHERE {' AND '.join(clauses)} "
                "ORDER BY priority DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            _row(row, json_fields=("blockers_json",))  # type: ignore[arg-type]
            for row in rows
        ]

    def _validate_transition(self, old_status: str, new_status: str) -> None:
        if new_status == "done":
            raise InvalidTransition("done can only be assigned by the validation completion gate")
        if new_status not in TASK_STATUSES or new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
            raise InvalidTransition(f"invalid task transition: {old_status} -> {new_status}")

    def _invalidate_task_evidence(
        self, connection: sqlite3.Connection, task_id: str, new_revision: int
    ) -> None:
        previous_checks = connection.execute(
            """
            SELECT * FROM project_memory_checks
            WHERE task_id = ? AND task_revision = ?
            ORDER BY created_at, id
            """,
            (task_id, new_revision - 1),
        ).fetchall()
        connection.execute(
            "UPDATE project_memory_evidence SET valid = 0 WHERE task_id = ? AND valid = 1",
            (task_id,),
        )
        connection.execute(
            """
            UPDATE project_memory_checks
            SET status = 'stale', updated_at = ?
            WHERE task_id = ? AND task_revision < ?
            """,
            (time.time(), task_id, new_revision),
        )
        now = time.time()
        for check in previous_checks:
            connection.execute(
                """
                INSERT INTO project_memory_checks
                    (id, task_id, title, check_type, required, status, task_revision,
                     config_json, last_evidence_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL, ?, ?)
                """,
                (_id("check"), task_id, check["title"], check["check_type"],
                 check["required"], new_revision, check["config_json"], now, now),
            )

    def update_task(
        self,
        task_id: str,
        *,
        base_revision: int,
        status: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        blockers: Optional[list[str]] = None,
        current_step_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ProjectMemoryError("task not found")
            if int(row["revision"]) != int(base_revision):
                raise RevisionConflict(f"task {task_id} revision changed")
            assignments: list[str] = []
            params: list[Any] = []
            specification_changed = False
            if status is not None and status != row["status"]:
                self._validate_transition(str(row["status"]), status)
                assignments.append("status = ?")
                params.append(status)
            if title is not None and str(title).strip() != row["title"]:
                if not str(title).strip():
                    raise ProjectMemoryError("task title is required")
                assignments.append("title = ?")
                params.append(str(title).strip())
                specification_changed = True
            if description is not None and str(description) != row["description"]:
                assignments.append("description = ?")
                params.append(str(description))
                specification_changed = True
            if blockers is not None:
                assignments.append("blockers_json = ?")
                params.append(_json(blockers, []))
            if current_step_id is not None:
                step = connection.execute(
                    "SELECT task_id FROM project_memory_steps WHERE id = ?", (current_step_id,)
                ).fetchone()
                if step is None or step["task_id"] != task_id:
                    raise ProjectMemoryError("current step does not belong to task")
                assignments.append("current_step_id = ?")
                params.append(current_step_id)
            if not assignments:
                task = self.get_task(task_id)
                assert task is not None
                return task
            new_revision = int(row["revision"]) + (1 if specification_changed else 0)
            if specification_changed:
                assignments.append("revision = ?")
                params.append(new_revision)
                self._invalidate_task_evidence(connection, task_id, new_revision)
                if row["status"] == "done" and status is None:
                    assignments.append("status = 'needs_review'")
                    assignments.append("completed_at = NULL")
            assignments.append("updated_at = ?")
            params.append(time.time())
            params.extend([task_id, int(base_revision)])
            cursor = connection.execute(
                f"UPDATE project_memory_tasks SET {', '.join(assignments)} "
                "WHERE id = ? AND revision = ?",
                params,
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RevisionConflict(f"task {task_id} revision changed")
            self._queue_event(connection, row["project_id"], "task.updated", {
                "task_id": task_id, "status": status, "specification_changed": specification_changed,
            })
            connection.commit()
        self.flush_events(str(row["project_id"]))
        task = self.get_task(task_id)
        assert task is not None
        return task

    def add_step(
        self, task_id: str, title: str, *, ordinal: Optional[int] = None, step_id: Optional[str] = None
    ) -> dict[str, Any]:
        name = str(title or "").strip()
        if not name:
            raise ProjectMemoryError("step title is required")
        identifier = step_id or _id("step")
        now = time.time()
        with closing(self.connect()) as connection:
            task = connection.execute(
                "SELECT * FROM project_memory_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ProjectMemoryError("task not found")
            if ordinal is None:
                max_row = connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) AS value FROM project_memory_steps WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                ordinal = int(max_row["value"]) + 1
            connection.execute(
                """
                INSERT INTO project_memory_steps
                    (id, task_id, ordinal, title, status, result_summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'planned', '', ?, ?)
                """,
                (identifier, task_id, int(ordinal), name, now, now),
            )
            new_revision = int(task["revision"]) + 1
            connection.execute(
                "UPDATE project_memory_tasks SET revision = ?, updated_at = ? WHERE id = ?",
                (new_revision, now, task_id),
            )
            self._invalidate_task_evidence(connection, task_id, new_revision)
            self._queue_event(connection, task["project_id"], "task.step.created", {
                "task_id": task_id, "step_id": identifier, "ordinal": ordinal, "title": name,
            })
            connection.commit()
        self.flush_events(str(task["project_id"]))
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM project_memory_steps WHERE id = ?", (identifier,)).fetchone()
        result = _row(row)
        assert result is not None
        return result

    def update_step(
        self, step_id: str, *, status: Optional[str] = None, result_summary: Optional[str] = None
    ) -> dict[str, Any]:
        valid_statuses = {"planned", "in_progress", "blocked", "done", "cancelled"}
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT s.*, t.project_id, t.revision AS task_revision,
                       t.status AS task_status FROM project_memory_steps s
                JOIN project_memory_tasks t ON t.id = s.task_id WHERE s.id = ?
                """,
                (step_id,),
            ).fetchone()
            if row is None:
                raise ProjectMemoryError("step not found")
            assignments: list[str] = []
            params: list[Any] = []
            if status is not None:
                if status not in valid_statuses:
                    raise InvalidTransition("invalid step status")
                if status != row["status"]:
                    assignments.append("status = ?")
                    params.append(status)
            if result_summary is not None and str(result_summary) != row["result_summary"]:
                assignments.append("result_summary = ?")
                params.append(str(result_summary))
            if assignments:
                now = time.time()
                assignments.append("updated_at = ?")
                params.extend([now, step_id])
                connection.execute(
                    f"UPDATE project_memory_steps SET {', '.join(assignments)} WHERE id = ?", params
                )
                new_revision = int(row["task_revision"]) + 1
                connection.execute(
                    """
                    UPDATE project_memory_tasks
                    SET revision=?, updated_at=?,
                        status=CASE WHEN status='done' THEN 'needs_review' ELSE status END,
                        completed_at=CASE WHEN status='done' THEN NULL ELSE completed_at END
                    WHERE id=?
                    """,
                    (new_revision, now, row["task_id"]),
                )
                self._invalidate_task_evidence(connection, row["task_id"], new_revision)
                self._queue_event(connection, row["project_id"], "task.step.updated", {
                    "task_id": row["task_id"], "step_id": step_id, "status": status,
                    "task_revision": new_revision,
                })
                connection.commit()
        self.flush_events(str(row["project_id"]))
        with closing(self.connect()) as connection:
            updated = connection.execute("SELECT * FROM project_memory_steps WHERE id = ?", (step_id,)).fetchone()
        result = _row(updated)
        assert result is not None
        return result

    def list_steps(self, task_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM project_memory_steps WHERE task_id = ? ORDER BY ordinal", (task_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_checks(self, task_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.* FROM project_memory_checks c
                JOIN project_memory_tasks t ON t.id = c.task_id
                WHERE c.task_id = ? AND c.task_revision = t.revision
                ORDER BY c.created_at, c.id
                """,
                (task_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["required"] = bool(item["required"])
            item["config"] = _decode(item.pop("config_json"), {})
            result.append(item)
        return result

    def replace_validation_plan(
        self, task_id: str, *, base_revision: int, checks: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        normalized = _normalize_checks(checks)
        now = time.time()
        with closing(self.connect()) as connection:
            task = connection.execute(
                "SELECT * FROM project_memory_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ProjectMemoryError("task not found")
            if int(task["revision"]) != int(base_revision):
                raise RevisionConflict(f"task {task_id} revision changed")
            new_revision = int(task["revision"]) + 1
            connection.execute(
                "UPDATE project_memory_evidence SET valid = 0 WHERE task_id = ? AND valid = 1",
                (task_id,),
            )
            connection.execute(
                "UPDATE project_memory_checks SET status = 'stale', updated_at = ? "
                "WHERE task_id = ? AND task_revision = ?",
                (now, task_id, int(task["revision"])),
            )
            for check in normalized:
                check_id = check["id"]
                exists = connection.execute(
                    "SELECT 1 FROM project_memory_checks WHERE id = ?", (check_id,)
                ).fetchone()
                if exists is not None:
                    check_id = _id("check")
                connection.execute(
                    """
                    INSERT INTO project_memory_checks
                        (id, task_id, title, check_type, required, status, task_revision,
                         config_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (check_id, task_id, check["title"], check["check_type"],
                     1 if check["required"] else 0, new_revision, _json(check["config"], {}), now, now),
                )
            connection.execute(
                "UPDATE project_memory_tasks SET revision = ?, updated_at = ?, "
                "status = CASE WHEN status = 'done' THEN 'needs_review' ELSE status END, completed_at = NULL WHERE id = ?",
                (new_revision, now, task_id),
            )
            self._queue_event(connection, task["project_id"], "validation.plan.replaced", {
                "task_id": task_id, "task_revision": new_revision, "checks": normalized,
            })
            connection.commit()
        self.flush_events(str(task["project_id"]))
        return self.list_checks(task_id)

    def record_validation_result(
        self,
        task_id: str,
        check_id: str,
        *,
        status: str,
        evidence_type: str,
        summary: str,
        task_revision: int,
        source_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        artifact_path: Optional[str] = None,
        artifact_fingerprint: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if status not in RESULT_STATUSES:
            raise ValidationEvidenceError("unsupported validation result status")
        note = str(summary or "").strip()
        if not note:
            raise ValidationEvidenceError("validation evidence summary is required")
        identifier = evidence_id or _id("evidence")
        now = time.time()
        with closing(self.connect()) as connection:
            task = connection.execute(
                "SELECT * FROM project_memory_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            check = connection.execute(
                "SELECT * FROM project_memory_checks WHERE id = ? AND task_id = ?", (check_id, task_id)
            ).fetchone()
            if task is None or check is None:
                raise ValidationEvidenceError("task or validation check not found")
            if int(task["revision"]) != int(task_revision) or int(check["task_revision"]) != int(task_revision):
                raise ValidationEvidenceError("validation evidence belongs to a stale task revision")
            existing = connection.execute(
                "SELECT * FROM project_memory_evidence WHERE id = ?", (identifier,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO project_memory_evidence
                        (id, project_id, task_id, check_id, task_revision, evidence_type,
                         source_id, tool_call_id, artifact_path, artifact_fingerprint,
                         summary, valid, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (identifier, task["project_id"], task_id, check_id, int(task_revision),
                     evidence_type or "unknown", source_id, tool_call_id, artifact_path,
                     artifact_fingerprint, note, now),
                )
            connection.execute(
                "UPDATE project_memory_checks SET status = ?, last_evidence_id = ?, updated_at = ? WHERE id = ?",
                (status, identifier, now, check_id),
            )
            self._queue_event(connection, task["project_id"], "validation.result.recorded", {
                "task_id": task_id, "check_id": check_id, "status": status,
                "evidence_id": identifier, "task_revision": task_revision,
            })
            connection.commit()
        self.flush_events(str(task["project_id"]))
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_evidence WHERE id = ?", (identifier,)
            ).fetchone()
        result = _row(row)
        assert result is not None
        return result

    def evaluate_task_completion(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        with closing(self.connect()) as connection:
            task = connection.execute(
                "SELECT * FROM project_memory_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ProjectMemoryError("task not found")
            checks = connection.execute(
                """
                SELECT * FROM project_memory_checks
                WHERE task_id = ? AND task_revision = ? AND required = 1
                """,
                (task_id, int(task["revision"])),
            ).fetchall()
            if not checks:
                raise ValidationPlanError("task has no required validation checks")
            children_open = connection.execute(
                """
                SELECT COUNT(1) AS total FROM project_memory_tasks
                WHERE parent_task_id = ? AND status NOT IN ('done','cancelled','superseded')
                """,
                (task_id,),
            ).fetchone()["total"]
            conflicts = connection.execute(
                """
                SELECT COUNT(1) AS total FROM project_memory_items
                WHERE task_id = ? AND kind = 'conflict' AND status = 'active'
                """,
                (task_id,),
            ).fetchone()["total"]
            statuses = {str(check["status"]) for check in checks}
            all_passed = all(check["status"] == "passed" for check in checks)
            evidence_valid = True
            for check in checks:
                evidence_id = check["last_evidence_id"]
                if not evidence_id:
                    evidence_valid = False
                    break
                evidence = connection.execute(
                    """
                    SELECT valid, task_revision FROM project_memory_evidence
                    WHERE id = ? AND task_id = ? AND check_id = ?
                    """,
                    (evidence_id, task_id, check["id"]),
                ).fetchone()
                if evidence is None or not evidence["valid"] or int(evidence["task_revision"]) != int(task["revision"]):
                    evidence_valid = False
                    break
            blockers = _decode(task["blockers_json"], [])
            can_complete = (
                task["status"] in {
                    "implementation_complete", "validating", "validation_failed",
                    "awaiting_user_validation", "needs_review", "done",
                }
                and all_passed and evidence_valid and not children_open and not blockers and not conflicts
            )
            if can_complete:
                new_status = "done"
                completed_at = task["completed_at"] or now
            elif "failed" in statuses:
                new_status, completed_at = "validation_failed", None
            elif "awaiting_user" in statuses:
                new_status, completed_at = "awaiting_user_validation", None
            elif task["status"] in {
                "implementation_complete", "validating", "validation_failed",
                "awaiting_user_validation", "needs_review",
            }:
                new_status, completed_at = "validating", None
            else:
                new_status, completed_at = str(task["status"]), task["completed_at"]
            if new_status != task["status"] or completed_at != task["completed_at"]:
                connection.execute(
                    "UPDATE project_memory_tasks SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                    (new_status, completed_at, now, task_id),
                )
                self._queue_event(connection, task["project_id"], "task.completion.evaluated", {
                    "task_id": task_id, "status": new_status, "completed": can_complete,
                    "children_open": int(children_open), "blockers": blockers,
                    "conflicts": int(conflicts),
                })
                connection.commit()
        self.flush_events(str(task["project_id"]))
        result = self.get_task(task_id)
        assert result is not None
        return result

    def derive_task_progress(self, task_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            task = connection.execute(
                "SELECT status, revision FROM project_memory_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                return {"completed_steps": 0, "total_steps": 0, "passed_required_checks": 0,
                        "total_required_checks": 0, "status": "missing"}
            steps = connection.execute(
                "SELECT COUNT(1) AS total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS complete "
                "FROM project_memory_steps WHERE task_id = ?", (task_id,)
            ).fetchone()
            checks = connection.execute(
                "SELECT COUNT(1) AS total, SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS complete "
                "FROM project_memory_checks WHERE task_id = ? AND task_revision = ? AND required = 1",
                (task_id, int(task["revision"])),
            ).fetchone()
        return {
            "completed_steps": int(steps["complete"] or 0),
            "total_steps": int(steps["total"] or 0),
            "passed_required_checks": int(checks["complete"] or 0),
            "total_required_checks": int(checks["total"] or 0),
            "status": task["status"],
        }

    def append_turn_memory(
        self,
        project_id: str,
        session_id: Optional[str],
        turn_id: str,
        *,
        base_revision: int,
        new_revision: int,
        user_summary: str,
        assistant_summary: str,
        delta: dict[str, Any],
        checkpoint_summary: str,
        status: str = "applied",
    ) -> dict[str, Any]:
        payload = _json(delta, {})
        now = time.time()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM project_memory_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["project_id"] == project_id
                    and existing["session_id"] == session_id
                    and int(existing["base_revision"]) == int(base_revision)
                    and int(existing["new_revision"]) == int(new_revision)
                    and existing["user_summary"] == str(user_summary or "")
                    and existing["assistant_summary"] == str(assistant_summary or "")
                    and existing["delta_json"] == payload
                    and existing["checkpoint_summary"] == str(checkpoint_summary or "")
                    and existing["status"] == status
                )
                if same:
                    result = _row(existing)
                    assert result is not None
                    result["delta"] = _decode(result.pop("delta_json"), {})
                    return result
                raise ProjectMemoryError("turn id already exists with different content")
            connection.execute(
                """
                INSERT INTO project_memory_turns
                    (turn_id, project_id, session_id, base_revision, new_revision,
                     user_summary, assistant_summary, delta_json, checkpoint_summary, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_id, project_id, session_id, int(base_revision), int(new_revision),
                 user_summary or "", assistant_summary or "", payload,
                 checkpoint_summary or "", status, now),
            )
            self._queue_event(connection, project_id, "project.turn.recorded", {
                "turn_id": turn_id, "session_id": session_id, "base_revision": base_revision,
                "new_revision": new_revision, "status": status,
            })
            connection.commit()
        self.flush_events(project_id)
        return self.get_turn_memory(turn_id) or {"turn_id": turn_id, "project_id": project_id}

    def get_turn_memory(self, turn_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        result = _row(row)
        if result is not None:
            result["delta"] = _decode(result.pop("delta_json"), {})
        return result

    def get_last_turn_memory(self, project_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_turns WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        result = _row(row)
        if result is not None:
            result["delta"] = _decode(result.pop("delta_json"), {})
        return result

    def add_memory_item(
        self,
        project_id: str,
        kind: str,
        content: str,
        *,
        task_id: Optional[str] = None,
        source_turn_id: Optional[str] = None,
        confidence: float = 0.0,
        status: str = "active",
        supersedes_id: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> dict[str, Any]:
        memory_kind = str(kind or "").strip()
        text = str(content or "").strip()
        if memory_kind not in MEMORY_KINDS or not text:
            raise ProjectMemoryError("invalid project memory item")
        identifier = item_id or _id("memory")
        now = time.time()
        with closing(self.connect()) as connection:
            if supersedes_id:
                connection.execute(
                    "UPDATE project_memory_items SET status='superseded', updated_at=? "
                    "WHERE id=? AND project_id=?",
                    (now, supersedes_id, project_id),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO project_memory_items
                    (id, project_id, kind, content, task_id, source_turn_id, confidence,
                     status, supersedes_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (identifier, project_id, memory_kind, text, task_id, source_turn_id,
                 max(0.0, min(1.0, float(confidence or 0.0))), status, supersedes_id, now, now),
            )
            self._queue_event(connection, project_id, "memory.item.added", {
                "item_id": identifier, "kind": memory_kind, "task_id": task_id,
            })
            connection.commit()
        self.flush_events(project_id)
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM project_memory_items WHERE id = ?", (identifier,)).fetchone()
        result = _row(row)
        assert result is not None
        return result

    def search_memory(
        self,
        project_id: str,
        query: str,
        *,
        kinds: Optional[Iterable[str]] = None,
        task_id: Optional[str] = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        wanted = {kind for kind in (kinds or []) if kind in MEMORY_KINDS}
        with closing(self.connect()) as connection:
            item_rows = connection.execute(
                """
                SELECT * FROM project_memory_items
                WHERE project_id = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT 500
                """,
                (project_id,),
            ).fetchall()
            turn_rows = connection.execute(
                """
                SELECT turn_id, project_id, user_summary, assistant_summary,
                       checkpoint_summary, created_at
                FROM project_memory_turns WHERE project_id = ?
                ORDER BY created_at DESC LIMIT 200
                """,
                (project_id,),
            ).fetchall()
        query_tokens = _tokens(query)
        now = time.time()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in item_rows:
            item = dict(row)
            if wanted and item["kind"] not in wanted:
                continue
            if task_id and item.get("task_id") not in {None, task_id}:
                continue
            overlap = len(query_tokens & _tokens(item["content"]))
            if query_tokens and overlap == 0:
                continue
            age_days = max(0.0, (now - float(item["updated_at"] or now)) / 86400.0)
            score = overlap * 3.0 + 1.0 / (1.0 + age_days / 30.0) + float(item["confidence"] or 0.0)
            if task_id and item.get("task_id") == task_id:
                score += 2.0
            item.update({"score": score, "source_type": "memory_item", "source_id": item["id"]})
            scored.append((score, item))
        if not wanted:
            for row in turn_rows:
                item = dict(row)
                content = "\n".join(str(item.get(key) or "") for key in (
                    "user_summary", "assistant_summary", "checkpoint_summary"
                ))
                overlap = len(query_tokens & _tokens(content))
                if query_tokens and overlap == 0:
                    continue
                age_days = max(0.0, (now - float(item["created_at"] or now)) / 86400.0)
                score = overlap * 2.0 + 1.0 / (1.0 + age_days / 30.0)
                item.update({
                    "kind": "turn", "content": content, "score": score,
                    "source_type": "turn", "source_id": item["turn_id"],
                })
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("source_id"))))
        return [item for _, item in scored[:_clamp_limit(limit)]]

    def create_memory_request(
        self,
        project_id: str,
        query: str,
        *,
        kinds: Optional[Iterable[str]] = None,
        task_id: Optional[str] = None,
        required: bool = False,
        reason: str = "",
        origin_turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        text = str(query or "").strip()
        if not text:
            raise ProjectMemoryError("memory request query is required")
        identifier = request_id or _id("memory_request")
        now = time.time()
        selected = [kind for kind in (kinds or []) if kind in MEMORY_KINDS]
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO project_memory_requests
                    (id, project_id, origin_turn_id, query, kinds_json, task_id,
                     required, reason, status, attempt_count, resolved_item_ids_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '[]', ?)
                """,
                (identifier, project_id, origin_turn_id, text, _json(selected, []), task_id,
                 1 if required else 0, reason or "", now),
            )
            self._queue_event(connection, project_id, "memory.request.created", {
                "request_id": identifier, "query": text, "required": bool(required),
            })
            connection.commit()
        self.flush_events(project_id)
        return self.get_memory_request(identifier) or {"id": identifier, "project_id": project_id}

    def get_memory_request(self, request_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_requests WHERE id = ?", (request_id,)
            ).fetchone()
        result = _row(row, json_fields=("kinds_json", "resolved_item_ids_json"))
        if result is not None:
            result["required"] = bool(result["required"])
        return result

    def list_pending_memory_requests(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM project_memory_requests
                WHERE project_id = ? AND status = 'pending'
                ORDER BY created_at LIMIT ?
                """,
                (project_id, _clamp_limit(limit, default=20, maximum=100)),
            ).fetchall()
        return [
            _row(row, json_fields=("kinds_json", "resolved_item_ids_json"))  # type: ignore[arg-type]
            for row in rows
        ]

    def resolve_memory_request(
        self, request_id: str, item_ids: Iterable[str], *, status: str = "resolved"
    ) -> dict[str, Any]:
        if status not in {"resolved", "failed", "cancelled"}:
            raise ProjectMemoryError("invalid memory request terminal status")
        now = time.time()
        resolved_ids = list(item_ids)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT project_id FROM project_memory_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ProjectMemoryError("memory request not found")
            connection.execute(
                """
                UPDATE project_memory_requests
                SET status=?, resolved_item_ids_json=?, resolved_at=?, attempt_count=attempt_count+1
                WHERE id=?
                """,
                (status, _json(resolved_ids, []), now, request_id),
            )
            self._queue_event(connection, row["project_id"], "memory.request.resolved", {
                "request_id": request_id, "status": status, "item_ids": resolved_ids,
            })
            connection.commit()
        self.flush_events(str(row["project_id"]))
        result = self.get_memory_request(request_id)
        assert result is not None
        return result

    def register_source(
        self, project_id: str, path: str | Path, *, source_turn_id: Optional[str] = None
    ) -> dict[str, Any]:
        original = str(path)
        candidate = Path(path).expanduser()
        try:
            normalized = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            normalized = os.path.normcase(str(candidate.absolute()))
        source_type = "directory" if candidate.is_dir() else "file"
        size: Optional[int] = None
        mtime_ns: Optional[int] = None
        availability = "missing"
        try:
            stat = candidate.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
            availability = "available"
        except FileNotFoundError:
            availability = "missing"
        except OSError:
            availability = "unreadable"
        now = time.time()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                "SELECT id FROM project_memory_sources WHERE project_id=? AND normalized_path=?",
                (project_id, normalized),
            ).fetchone()
            identifier = str(existing["id"]) if existing is not None else _id("source")
            connection.execute(
                """
                INSERT INTO project_memory_sources
                    (id, project_id, original_path, normalized_path, source_type, size,
                     mtime_ns, availability, source_turn_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, normalized_path) DO UPDATE SET
                    original_path=excluded.original_path, source_type=excluded.source_type,
                    size=excluded.size, mtime_ns=excluded.mtime_ns,
                    availability=excluded.availability,
                    source_turn_id=COALESCE(excluded.source_turn_id, project_memory_sources.source_turn_id),
                    updated_at=excluded.updated_at
                """,
                (identifier, project_id, original, normalized, source_type, size, mtime_ns,
                 availability, source_turn_id, now, now),
            )
            self._queue_event(connection, project_id, "source.registered", {
                "source_id": identifier, "path": normalized, "availability": availability,
            })
            connection.commit()
        self.flush_events(project_id)
        return self.get_source(identifier) or {"id": identifier, "project_id": project_id}

    def get_source(self, source_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_sources WHERE id = ?", (source_id,)
            ).fetchone()
        return _row(row)

    def list_sources(self, project_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM project_memory_sources WHERE project_id=? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _file_hash(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def invalidate_evidence_for_source(self, source_id: str) -> int:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT e.task_id, e.project_id, t.revision, t.status
                FROM project_memory_evidence e
                JOIN project_memory_tasks t ON t.id = e.task_id
                WHERE e.source_id=? AND e.valid=1
                """,
                (source_id,),
            ).fetchall()
            for row in rows:
                new_revision = int(row["revision"]) + 1
                self._invalidate_task_evidence(connection, row["task_id"], new_revision)
                connection.execute(
                    """
                    UPDATE project_memory_tasks
                    SET revision=?,
                        status=CASE WHEN status='done' THEN 'needs_review' ELSE status END,
                        completed_at=CASE WHEN status='done' THEN NULL ELSE completed_at END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (new_revision, time.time(), row["task_id"]),
                )
                self._queue_event(connection, row["project_id"], "source.evidence.invalidated", {
                    "source_id": source_id, "task_id": row["task_id"],
                    "task_revision": new_revision,
                })
            connection.commit()
        for row in rows:
            self.flush_events(str(row["project_id"]))
        return len(rows)

    def refresh_source(self, source_id: str, *, compute_hash: bool = False) -> dict[str, Any]:
        source = self.get_source(source_id)
        if source is None:
            raise ProjectMemoryError("source not found")
        path = Path(source["normalized_path"])
        size: Optional[int] = None
        mtime_ns: Optional[int] = None
        content_hash = source.get("content_hash")
        availability = "missing"
        changed = False
        try:
            stat = path.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
            changed = source.get("size") != size or source.get("mtime_ns") != mtime_ns
            availability = "changed" if changed else "available"
            if compute_hash and path.is_file():
                new_hash = self._file_hash(path)
                changed = bool(content_hash and content_hash != new_hash) or changed
                content_hash = new_hash
                availability = "changed" if changed else "available"
        except FileNotFoundError:
            availability = "missing"
            changed = source.get("availability") == "available"
        except OSError:
            availability = "unreadable"
        now = time.time()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE project_memory_sources
                SET size=?, mtime_ns=?, content_hash=?, availability=?, updated_at=? WHERE id=?
                """,
                (size, mtime_ns, content_hash, availability, now, source_id),
            )
            self._queue_event(connection, source["project_id"], "source.refreshed", {
                "source_id": source_id, "availability": availability, "changed": changed,
            })
            connection.commit()
        if changed:
            self.invalidate_evidence_for_source(source_id)
        self.flush_events(source["project_id"])
        result = self.get_source(source_id)
        assert result is not None
        return result

    def save_resume(
        self, project_id: str, session_id: str, messages: list[dict[str, Any]], prompt_fingerprint: str
    ) -> None:
        if not session_id:
            raise ProjectMemoryError("session_id is required for resume state")
        now = time.time()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO project_memory_resume
                    (project_id, session_id, messages_json, prompt_fingerprint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, session_id) DO UPDATE SET
                    messages_json=excluded.messages_json,
                    prompt_fingerprint=excluded.prompt_fingerprint,
                    updated_at=excluded.updated_at
                """,
                (project_id, session_id, _json(messages, []), prompt_fingerprint or "", now, now),
            )
            self._queue_event(connection, project_id, "project.resume.saved", {"session_id": session_id})
            connection.commit()
        self.flush_events(project_id)

    def load_resume(self, project_id: str, session_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_resume WHERE project_id=? AND session_id=?",
                (project_id, session_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        messages = _decode(result.pop("messages_json"), [])
        if not messages:
            return None
        result["messages"] = messages
        return result

    def clear_resume(self, project_id: str, session_id: str) -> None:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM project_memory_resume WHERE project_id=? AND session_id=?",
                (project_id, session_id),
            )
            if cursor.rowcount:
                self._queue_event(connection, project_id, "project.resume.cleared", {"session_id": session_id})
            connection.commit()
        self.flush_events(project_id)

    def delete_project_memory(self, project_id: str) -> None:
        with closing(self.connect()) as connection:
            task_ids = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM project_memory_tasks WHERE project_id=?", (project_id,)
                ).fetchall()
            ]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                connection.execute(f"DELETE FROM project_memory_evidence WHERE task_id IN ({placeholders})", task_ids)
                connection.execute(f"DELETE FROM project_memory_checks WHERE task_id IN ({placeholders})", task_ids)
                connection.execute(f"DELETE FROM project_memory_steps WHERE task_id IN ({placeholders})", task_ids)
            for table in (
                "project_memory_tasks", "project_memory_turns", "project_memory_items",
                "project_memory_requests", "project_memory_sources", "project_memory_resume",
                "project_memory_events", "project_memory_state",
            ):
                connection.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            connection.commit()
