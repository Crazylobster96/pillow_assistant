"""Audited category assignments and resumable local category migration."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Optional


def _connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def ensure_graph_admin_schema(db_path: str | Path) -> None:
    with closing(_connect(db_path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_graph_assignments (
                project_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
                category_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(project_id,subject_type,subject_id),
                FOREIGN KEY(category_id) REFERENCES project_graph_categories(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_graph_jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_type TEXT NOT NULL,
                source_category_id TEXT, target_category_id TEXT, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_jobs_status ON project_graph_jobs(project_id,status,updated_at)"
        )
        connection.commit()


def assign_category(
    federation: Any,
    project_id: str,
    subject_type: str,
    subject_id: str,
    category_id: str,
    *,
    reason: str = "",
    base_revision: Optional[int] = None,
) -> dict[str, Any]:
    category = federation._get_category(category_id)
    if category is None or category["project_id"] != project_id:
        raise ValueError("assigned category must belong to project")
    kind, identifier = str(subject_type or "").strip(), str(subject_id or "").strip()
    if not kind or not identifier:
        raise ValueError("assignment subject type and id are required")
    now = time.time()
    with closing(_connect(federation.db_path)) as connection:
        existing = connection.execute(
            """
            SELECT * FROM project_graph_assignments
            WHERE project_id=? AND subject_type=? AND subject_id=?
            """,
            (project_id, kind, identifier),
        ).fetchone()
        if base_revision is not None and (
            existing is None or int(existing["revision"]) != int(base_revision)
        ):
            raise ValueError("category assignment revision changed")
        revision = int(existing["revision"]) + 1 if existing is not None else 1
        connection.execute(
            """
            INSERT INTO project_graph_assignments
                (project_id,subject_type,subject_id,category_id,revision,reason,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id,subject_type,subject_id) DO UPDATE SET
                category_id=excluded.category_id,revision=excluded.revision,
                reason=excluded.reason,updated_at=excluded.updated_at
            """,
            (project_id, kind, identifier, category_id, revision, str(reason or ""), now, now),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT * FROM project_graph_assignments
            WHERE project_id=? AND subject_type=? AND subject_id=?
            """,
            (project_id, kind, identifier),
        ).fetchone()
    return dict(row)


def get_assignment(
    federation: Any, project_id: str, subject_type: str, subject_id: str
) -> Optional[dict[str, Any]]:
    with closing(_connect(federation.db_path)) as connection:
        row = connection.execute(
            """
            SELECT * FROM project_graph_assignments
            WHERE project_id=? AND subject_type=? AND subject_id=?
            """,
            (project_id, subject_type, subject_id),
        ).fetchone()
    return dict(row) if row is not None else None


def resolve_category(
    federation: Any,
    project_id: str,
    text: str,
    *,
    subject_type: Optional[str] = None,
    subject_id: Optional[str] = None,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    if subject_type and subject_id:
        assignment = get_assignment(federation, project_id, subject_type, subject_id)
        if assignment:
            category = federation._get_category(str(assignment["category_id"]))
            if category:
                return [{
                    "category": category, "score": 1.0, "reason": "manual-assignment",
                    "profile_id": "manual", "assignment_revision": assignment["revision"],
                }]
    return federation.classify(project_id, text, top_k=top_k)


def migrate_category(federation: Any, source_category_id: str, target_category_id: str) -> dict[str, Any]:
    source = federation._get_category(source_category_id)
    target = federation._get_category(target_category_id)
    if source is None or target is None:
        raise ValueError("migration categories must exist")
    if source["project_id"] != target["project_id"] or source_category_id == target_category_id:
        raise ValueError("migration categories must be different and in the same project")
    children = federation.list_categories(str(source["project_id"]), parent_id=source_category_id)
    if children:
        raise ValueError("migrate child categories separately before migrating their parent")
    project_id = str(source["project_id"])
    job_id = f"graphjob_{uuid.uuid5(uuid.NAMESPACE_URL, source_category_id + ':' + target_category_id).hex}"
    now = time.time()
    with closing(_connect(federation.db_path)) as connection:
        connection.execute(
            """
            INSERT INTO project_graph_jobs
                (id,project_id,job_type,source_category_id,target_category_id,status,
                 attempt_count,payload_json,created_at,updated_at)
            VALUES (?,?, 'category_migration',?,?,'running',1,'{}',?,?)
            ON CONFLICT(id) DO UPDATE SET status='running',attempt_count=attempt_count+1,
                last_error=NULL,updated_at=excluded.updated_at
            """,
            (job_id, project_id, source_category_id, target_category_id, now, now),
        )
        connection.commit()
    try:
        with closing(federation.connect()) as connection:
            node_rows = connection.execute(
                "SELECT id FROM project_graph_nodes WHERE category_id=? ORDER BY id",
                (source_category_id,),
            ).fetchall()
            edge_rows = connection.execute(
                "SELECT * FROM project_graph_edges WHERE category_id=? AND validity='active' ORDER BY id",
                (source_category_id,),
            ).fetchall()
        old_nodes = [federation.get_node(str(row["id"])) for row in node_rows]
        mapping: dict[str, str] = {}
        for node in old_nodes:
            if node is None:
                continue
            node_key = str(node["node_key"])
            with closing(federation.connect()) as connection:
                conflict = connection.execute(
                    "SELECT fingerprint FROM project_graph_nodes WHERE category_id=? AND node_key=?",
                    (target_category_id, node_key),
                ).fetchone()
            if conflict is not None and conflict["fingerprint"] != node["fingerprint"]:
                node_key = f"{source_category_id}:{node_key}"
            migrated = federation.upsert_node(
                project_id, target_category_id, node_key, node_type=node["node_type"],
                label=node["label"], content=node["content"], source_id=node.get("source_id"),
                document_id=node.get("document_id"), chunk_id=node.get("chunk_id"),
                task_id=node.get("task_id"), turn_id=node.get("turn_id"),
                fingerprint=node["fingerprint"],
                provenance={**(node.get("provenance") or {}),
                            "migrated_from_category": source_category_id,
                            "migration_job_id": job_id},
            )
            mapping[str(node["id"])] = str(migrated["id"])
        for edge_row in edge_rows:
            edge = dict(edge_row)
            federation.add_edge(
                project_id, target_category_id, mapping[edge["from_node_id"]],
                mapping[edge["to_node_id"]], edge["relation_type"],
                directed=bool(edge["directed"]), weight=float(edge["weight"]),
                confidence=float(edge["confidence"]),
                evidence=json.loads(edge["evidence_json"] or "{}"),
                allow_self=edge["from_node_id"] == edge["to_node_id"],
            )
        last_link_id = ""
        while True:
            with closing(federation.connect()) as connection:
                link_rows = connection.execute(
                    """
                    SELECT * FROM project_graph_cross_links
                    WHERE project_id=? AND validity='active' AND id>?
                      AND (from_category_id=? OR to_category_id=?)
                    ORDER BY id LIMIT 500
                    """,
                    (project_id, last_link_id, source_category_id, source_category_id),
                ).fetchall()
            if not link_rows:
                break
            for link_row in link_rows:
                link = dict(link_row)
                from_id = mapping.get(link["from_node_id"], link["from_node_id"])
                to_id = mapping.get(link["to_node_id"], link["to_node_id"])
                if from_id == link["from_node_id"] and to_id == link["to_node_id"]:
                    raise ValueError("migration cross-link does not reference a migrated node")
                evidence = json.loads(link.pop("evidence_json") or "{}")
                from_node, to_node = federation.get_node(from_id), federation.get_node(to_id)
                if from_node and to_node and from_node["category_id"] == to_node["category_id"]:
                    federation.add_edge(
                        project_id, from_node["category_id"], from_id, to_id,
                        link["relation_type"], weight=float(link["weight"]),
                        evidence=evidence,
                    )
                else:
                    federation.add_cross_link(
                        project_id, from_id, to_id, link["relation_type"],
                        weight=float(link["weight"]), evidence=evidence,
                    )
            last_link_id = str(link_rows[-1]["id"])
        with closing(_connect(federation.db_path)) as connection:
            connection.execute(
                """
                UPDATE project_graph_assignments
                SET category_id=?,revision=revision+1,
                    reason=CASE WHEN reason='' THEN ? ELSE reason || '; ' || ? END,
                    updated_at=?
                WHERE project_id=? AND category_id=?
                """,
                (target_category_id, f"migration:{job_id}", f"migration:{job_id}",
                 time.time(), project_id, source_category_id),
            )
            connection.commit()
        deleted = federation.delete_category(
            source_category_id, cascade=True,
            replacement_inbox_id=target_category_id if source.get("is_inbox") else None,
        )
        payload = {"node_mapping": mapping, "deleted": deleted}
        with closing(_connect(federation.db_path)) as connection:
            connection.execute(
                "UPDATE project_graph_jobs SET status='done',payload_json=?,updated_at=? WHERE id=?",
                (_json(payload), time.time(), job_id),
            )
            connection.commit()
        return {"job_id": job_id, "status": "done", **payload}
    except Exception as exc:
        with closing(_connect(federation.db_path)) as connection:
            connection.execute(
                "UPDATE project_graph_jobs SET status='failed',last_error=?,updated_at=? WHERE id=?",
                (str(exc)[:1000], time.time(), job_id),
            )
            connection.commit()
        raise
