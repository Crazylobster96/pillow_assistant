"""Category-isolated graph + vector federation for Level-3 project memory."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

from pillow_assistant.core.rag.graph_provider import validate_graph_provider
from pillow_assistant.core.rag.local_hybrid import LocalHybridRAG, fingerprint_text


class GraphFederationError(Exception):
    pass


class CategoryCycleError(GraphFederationError):
    pass


class CrossCategoryEdgeError(GraphFederationError):
    pass


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _json(value: Any, default: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(default, ensure_ascii=False, separators=(",", ":"))


def _decode(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


class FederatedGraphRAG:
    def __init__(
        self, db_path: str | Path, *, embedding: Any = None,
        providers: Optional[dict[str, Any]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.rag = LocalHybridRAG(self.db_path, embedding=embedding)
        self.providers = dict(providers or {})
        for provider_id, provider in self.providers.items():
            validate_graph_provider(provider, provider_id)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def ensure_schema(self) -> None:
        self.rag.ensure_schema()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS project_graph_categories (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', parent_id TEXT,
                routing_examples_json TEXT NOT NULL DEFAULT '[]', backend_id TEXT NOT NULL,
                modalities_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active',
                revision INTEGER NOT NULL DEFAULT 1, is_inbox INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(project_id,name),
                FOREIGN KEY(parent_id) REFERENCES project_graph_categories(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_graph_nodes (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, category_id TEXT NOT NULL,
                node_key TEXT NOT NULL, node_type TEXT NOT NULL, label TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '', source_id TEXT, document_id TEXT, chunk_id TEXT,
                task_id TEXT, turn_id TEXT, fingerprint TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                validity TEXT NOT NULL DEFAULT 'active', provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(category_id,node_key),
                FOREIGN KEY(category_id) REFERENCES project_graph_categories(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_graph_edges (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, category_id TEXT NOT NULL,
                from_node_id TEXT NOT NULL, to_node_id TEXT NOT NULL, relation_type TEXT NOT NULL,
                directed INTEGER NOT NULL DEFAULT 1, weight REAL NOT NULL DEFAULT 1,
                confidence REAL NOT NULL DEFAULT 0.5, evidence_json TEXT NOT NULL DEFAULT '{}',
                validity TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(category_id,from_node_id,to_node_id,relation_type,directed),
                FOREIGN KEY(category_id) REFERENCES project_graph_categories(id) ON DELETE CASCADE,
                FOREIGN KEY(from_node_id) REFERENCES project_graph_nodes(id) ON DELETE CASCADE,
                FOREIGN KEY(to_node_id) REFERENCES project_graph_nodes(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_graph_cross_links (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                from_category_id TEXT NOT NULL, from_node_id TEXT NOT NULL,
                to_category_id TEXT NOT NULL, to_node_id TEXT NOT NULL,
                relation_type TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1,
                evidence_json TEXT NOT NULL DEFAULT '{}', validity TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(from_node_id,to_node_id,relation_type),
                FOREIGN KEY(from_node_id) REFERENCES project_graph_nodes(id) ON DELETE CASCADE,
                FOREIGN KEY(to_node_id) REFERENCES project_graph_nodes(id) ON DELETE CASCADE
            )
            """,
        )
        indexes = (
            "CREATE INDEX IF NOT EXISTS idx_graph_cat_project ON project_graph_categories(project_id,parent_id,status)",
            "CREATE INDEX IF NOT EXISTS idx_graph_node_cat ON project_graph_nodes(category_id,node_type,validity)",
            "CREATE INDEX IF NOT EXISTS idx_graph_edge_cat_from ON project_graph_edges(category_id,from_node_id,validity)",
            "CREATE INDEX IF NOT EXISTS idx_graph_edge_cat_to ON project_graph_edges(category_id,to_node_id,validity)",
            "CREATE INDEX IF NOT EXISTS idx_graph_cross_project ON project_graph_cross_links(project_id,validity)",
        )
        with closing(self.connect()) as connection:
            for statement in statements:
                connection.execute(statement)
            for statement in indexes:
                connection.execute(statement)
            connection.commit()
        from pillow_assistant.core.rag.graph_admin import ensure_graph_admin_schema
        ensure_graph_admin_schema(self.db_path)

    def health(self) -> dict[str, Any]:
        result = self.rag.health()
        result["graph"] = True
        result["providers"] = {}
        for provider_id, provider in self.providers.items():
            try:
                result["providers"][provider_id] = provider.health()
            except Exception as exc:
                result["providers"][provider_id] = {"ok": False, "error": str(exc)}
        return result

    def assign_category(
        self, project_id: str, subject_type: str, subject_id: str, category_id: str,
        *, reason: str = "", base_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        from pillow_assistant.core.rag.graph_admin import assign_category
        return assign_category(
            self, project_id, subject_type, subject_id, category_id,
            reason=reason, base_revision=base_revision,
        )

    def get_assignment(
        self, project_id: str, subject_type: str, subject_id: str,
    ) -> Optional[dict[str, Any]]:
        from pillow_assistant.core.rag.graph_admin import get_assignment
        return get_assignment(self, project_id, subject_type, subject_id)

    def resolve_category(
        self, project_id: str, text: str, *, subject_type: Optional[str] = None,
        subject_id: Optional[str] = None, top_k: int = 3,
    ) -> list[dict[str, Any]]:
        from pillow_assistant.core.rag.graph_admin import resolve_category
        return resolve_category(
            self, project_id, text, subject_type=subject_type,
            subject_id=subject_id, top_k=top_k,
        )

    def migrate_category(self, source_category_id: str, target_category_id: str) -> dict[str, Any]:
        from pillow_assistant.core.rag.graph_admin import migrate_category
        return migrate_category(self, source_category_id, target_category_id)


    def _provider_for(self, category: dict[str, Any]) -> Any:
        provider_id = str(category.get("backend_id") or "local")
        if provider_id == "local":
            return None
        provider = self.providers.get(provider_id)
        if provider is None:
            raise GraphFederationError(f"category provider '{provider_id}' is not installed")
        return provider

    @staticmethod
    def _route_namespace(project_id: str) -> str:
        return f"{project_id}::graph-routes"

    @staticmethod
    def _category_namespace(project_id: str, category_id: str) -> str:
        return f"{project_id}::category::{category_id}"

    def _get_category(self, category_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_graph_categories WHERE id=?", (category_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["routing_examples"] = _decode(item.pop("routing_examples_json"), [])
        item["modalities"] = _decode(item.pop("modalities_json"), [])
        item["is_inbox"] = bool(item["is_inbox"])
        return item

    def list_categories(self, project_id: str, parent_id: Any = ...) -> list[dict[str, Any]]:
        clauses = ["project_id=?", "status='active'"]
        params: list[Any] = [project_id]
        if parent_id is None:
            clauses.append("parent_id IS NULL")
        elif parent_id is not ...:
            clauses.append("parent_id=?")
            params.append(parent_id)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"SELECT id FROM project_graph_categories WHERE {' AND '.join(clauses)} ORDER BY name",
                params,
            ).fetchall()
        return [self._get_category(str(row["id"])) for row in rows]

    def register_category(
        self,
        project_id: str,
        name: str,
        *,
        description: str = "",
        parent_id: Optional[str] = None,
        routing_examples: Optional[Iterable[str]] = None,
        backend_id: str = "local",
        modalities: Optional[Iterable[str]] = None,
        is_inbox: bool = False,
        category_id: Optional[str] = None,
    ) -> dict[str, Any]:
        title = str(name or "").strip()
        if not title:
            raise GraphFederationError("category name is required")
        selected_backend = str(backend_id or "local")
        if selected_backend != "local" and selected_backend not in self.providers:
            raise GraphFederationError(f"category provider '{selected_backend}' is not installed")
        if parent_id:
            parent = self._get_category(parent_id)
            if parent is None or parent["project_id"] != project_id:
                raise GraphFederationError("category parent must belong to the same project")
        identifier = category_id or _stable_id("category", project_id, title)
        examples = [str(item).strip() for item in (routing_examples or []) if str(item).strip()][:50]
        modes = [str(item).strip() for item in (modalities or ["text"]) if str(item).strip()][:20]
        now = time.time()
        with closing(self.connect()) as connection:
            if is_inbox:
                inbox = connection.execute(
                    "SELECT id FROM project_graph_categories WHERE project_id=? AND is_inbox=1 AND status='active'",
                    (project_id,),
                ).fetchone()
                if inbox is not None and inbox["id"] != identifier:
                    raise GraphFederationError("a project can have only one active inbox category")
            existing = connection.execute(
                "SELECT id FROM project_graph_categories WHERE project_id=? AND name=?",
                (project_id, title),
            ).fetchone()
            if existing is not None:
                found = self._get_category(str(existing["id"]))
                assert found is not None
                return found
            connection.execute(
                """
                INSERT INTO project_graph_categories
                    (id,project_id,name,description,parent_id,routing_examples_json,backend_id,
                     modalities_json,status,revision,is_inbox,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'active',1,?,?,?)
                """,
                (identifier, project_id, title, str(description or ""), parent_id,
                 _json(examples, []), selected_backend, _json(modes, []),
                 1 if is_inbox else 0, now, now),
            )
            connection.commit()
        category = self._get_category(identifier)
        assert category is not None
        self._index_category_route(category)
        return category

    def _index_category_route(self, category: dict[str, Any]) -> None:
        text = "\n".join([
            str(category.get("name") or ""), str(category.get("description") or ""),
            *[str(item) for item in category.get("routing_examples") or []],
        ])
        parent_key = str(category.get("parent_id") or "__root__")
        self.rag.index_internal(
            self._route_namespace(str(category["project_id"])), str(category["id"]), text,
            kind="category_route", task_id=parent_key,
            fingerprint=fingerprint_text(text + f"\nrevision={category.get('revision')}")
        )

    def _assert_no_cycle(
        self, project_id: str, category_id: str, candidate_parent_id: Optional[str]
    ) -> None:
        current = candidate_parent_id
        for _ in range(100):
            if current is None:
                return
            if current == category_id:
                raise CategoryCycleError("category parent would create a cycle")
            parent = self._get_category(current)
            if parent is None or parent["project_id"] != project_id:
                raise GraphFederationError("category parent must belong to the same project")
            current = parent.get("parent_id")
        raise CategoryCycleError("category hierarchy exceeds 100 levels")

    def update_category(
        self, category_id: str, *, base_revision: int, name: Optional[str] = None,
        description: Optional[str] = None, parent_id: Any = ...,
        routing_examples: Optional[Iterable[str]] = None,
    ) -> dict[str, Any]:
        category = self._get_category(category_id)
        if category is None:
            raise GraphFederationError("category not found")
        if int(category["revision"]) != int(base_revision):
            raise GraphFederationError("category revision changed")
        target_parent = category.get("parent_id") if parent_id is ... else parent_id
        self._assert_no_cycle(str(category["project_id"]), category_id, target_parent)
        title = str(name).strip() if name is not None else category["name"]
        if not title:
            raise GraphFederationError("category name is required")
        examples = (
            [str(item).strip() for item in routing_examples if str(item).strip()][:50]
            if routing_examples is not None else category["routing_examples"]
        )
        new_revision = int(base_revision) + 1
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE project_graph_categories SET name=?,description=?,parent_id=?,
                    routing_examples_json=?,revision=?,updated_at=? WHERE id=? AND revision=?
                """,
                (title, str(description) if description is not None else category["description"],
                 target_parent, _json(examples, []), new_revision, time.time(),
                 category_id, int(base_revision)),
            )
            if cursor.rowcount != 1:
                raise GraphFederationError("category revision changed")
            connection.commit()
        updated = self._get_category(category_id)
        assert updated is not None
        self._index_category_route(updated)
        return updated

    def _route_hit_category(self, hit: Any) -> Optional[dict[str, Any]]:
        with closing(self.rag.connect()) as connection:
            row = connection.execute(
                "SELECT document_key FROM project_rag_documents WHERE id=?", (hit.document_id,)
            ).fetchone()
        return self._get_category(str(row["document_key"])) if row else None

    @staticmethod
    def _route_relevance(hit: Any) -> float:
        lexical = 1.0 - math.exp(-max(0.0, float(hit.lexical_score)))
        vector = max(0.0, min(1.0, float(hit.vector_score)))
        return lexical * 0.45 + vector * 0.55

    def _inbox(self, project_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT id FROM project_graph_categories WHERE project_id=? AND is_inbox=1 AND status='active'",
                (project_id,),
            ).fetchone()
        return self._get_category(str(row["id"])) if row else None

    def classify(
        self, project_id: str, text: str, *, top_k: int = 3,
        threshold: float = 0.15, fanout_margin: float = 0.08,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(5, int(top_k)))
        roots = self.rag.search(
            self._route_namespace(project_id), text, task_id="__root__",
            kinds=["category_route"], top_k=limit, per_source_limit=1,
        )
        root_routes: list[tuple[dict[str, Any], float]] = []
        for hit in roots:
            category = self._route_hit_category(hit)
            if category and not category.get("is_inbox"):
                root_routes.append((category, self._route_relevance(hit)))
        if not root_routes or root_routes[0][1] < float(threshold):
            inbox = self._inbox(project_id)
            return ([{"category": inbox, "score": 0.0, "reason": "below-threshold-inbox",
                      "profile_id": self.rag.profile.profile_id}] if inbox else [])
        best = root_routes[0][1]
        selected = [item for item in root_routes if best - item[1] <= float(fanout_margin)][:limit]
        routes: list[dict[str, Any]] = []
        for root, root_score in selected:
            children = self.rag.search(
                self._route_namespace(project_id), text, task_id=str(root["id"]),
                kinds=["category_route"], top_k=limit, per_source_limit=1,
            )
            child_routes = []
            for hit in children:
                category = self._route_hit_category(hit)
                if category:
                    child_routes.append((category, self._route_relevance(hit)))
            if child_routes and child_routes[0][1] >= float(threshold):
                child_best = child_routes[0][1]
                for child, child_score in child_routes:
                    if child_best - child_score <= float(fanout_margin):
                        routes.append({
                            "category": child, "score": root_score * 0.35 + child_score * 0.65,
                            "reason": f"hierarchical:{root['id']}",
                            "profile_id": self.rag.profile.profile_id,
                        })
            else:
                routes.append({"category": root, "score": root_score, "reason": "top-level",
                               "profile_id": self.rag.profile.profile_id})
        routes.sort(key=lambda item: (-item["score"], item["category"]["id"]))
        return routes[:limit]

    def upsert_node(
        self,
        project_id: str,
        category_id: str,
        node_key: str,
        *,
        node_type: str,
        label: str,
        content: str = "",
        source_id: Optional[str] = None,
        document_id: Optional[str] = None,
        chunk_id: Optional[str] = None,
        task_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        fingerprint: Optional[str] = None,
        provenance: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        category = self._get_category(category_id)
        if category is None or category["project_id"] != project_id:
            raise GraphFederationError("node category must belong to project")
        key = str(node_key or "").strip()
        title = str(label or "").strip()
        if not key or not title:
            raise GraphFederationError("node key and label are required")
        body = str(content or "")
        digest = fingerprint or fingerprint_text(title + "\n" + body)
        identifier = _stable_id("graphnode", category_id, key)
        now = time.time()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM project_graph_nodes WHERE category_id=? AND node_key=?",
                (category_id, key),
            ).fetchone()
            if existing is not None and existing["fingerprint"] == digest:
                return self.get_node(str(existing["id"]))
            revision = int(existing["revision"]) + 1 if existing is not None else 1
            connection.execute(
                """
                INSERT INTO project_graph_nodes
                    (id,project_id,category_id,node_key,node_type,label,content,source_id,
                     document_id,chunk_id,task_id,turn_id,fingerprint,revision,validity,
                     provenance_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)
                ON CONFLICT(category_id,node_key) DO UPDATE SET
                    node_type=excluded.node_type,label=excluded.label,content=excluded.content,
                    source_id=excluded.source_id,document_id=excluded.document_id,
                    chunk_id=excluded.chunk_id,task_id=excluded.task_id,turn_id=excluded.turn_id,
                    fingerprint=excluded.fingerprint,revision=excluded.revision,validity='active',
                    provenance_json=excluded.provenance_json,updated_at=excluded.updated_at
                """,
                (identifier, project_id, category_id, key, str(node_type or "entity"), title,
                 body, source_id, document_id, chunk_id, task_id, turn_id, digest, revision,
                 _json(provenance or {}, {}), now, now),
            )
            connection.commit()
        node = self.get_node(identifier)
        assert node is not None
        provider = self._provider_for(category)
        try:
            if provider is None:
                self.rag.index_internal(
                    self._category_namespace(project_id, category_id), identifier,
                    title + "\n" + body, kind=str(node_type or "entity"), task_id=task_id,
                    fingerprint=digest,
                )
            else:
                provider.upsert_node(project_id, category_id, node)
        except Exception:
            with closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE project_graph_nodes SET validity='index_failed' WHERE id=?", (identifier,)
                )
                connection.commit()
        return node

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_graph_nodes WHERE id=?", (node_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["provenance"] = _decode(item.pop("provenance_json"), {})
        return item

    def add_edge(
        self,
        project_id: str,
        category_id: str,
        from_node_id: str,
        to_node_id: str,
        relation_type: str,
        *,
        directed: bool = True,
        weight: float = 1.0,
        confidence: float = 0.5,
        evidence: Optional[dict[str, Any]] = None,
        allow_self: bool = False,
    ) -> dict[str, Any]:
        source, target = self.get_node(from_node_id), self.get_node(to_node_id)
        if source is None or target is None:
            raise GraphFederationError("edge endpoints must exist")
        if source["project_id"] != project_id or target["project_id"] != project_id:
            raise GraphFederationError("edge endpoints must belong to project")
        if source["category_id"] != category_id or target["category_id"] != category_id:
            raise CrossCategoryEdgeError("ordinary graph edges cannot cross categories")
        if from_node_id == to_node_id and not allow_self:
            raise GraphFederationError("self edge requires allow_self")
        relation = str(relation_type or "").strip()
        if not relation:
            raise GraphFederationError("relation type is required")
        evidence_value = evidence or {}
        confidence_value = max(0.0, min(1.0, float(confidence)))
        if not evidence_value:
            confidence_value = min(0.5, confidence_value)
        identifier = _stable_id(
            "graphedge", category_id, from_node_id, to_node_id, relation, int(bool(directed))
        )
        now = time.time()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                "SELECT revision FROM project_graph_edges WHERE id=?", (identifier,)
            ).fetchone()
            revision = int(existing["revision"]) + 1 if existing else 1
            connection.execute(
                """
                INSERT INTO project_graph_edges
                    (id,project_id,category_id,from_node_id,to_node_id,relation_type,directed,
                     weight,confidence,evidence_json,validity,revision,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?,?)
                ON CONFLICT(id) DO UPDATE SET weight=excluded.weight,confidence=excluded.confidence,
                    evidence_json=excluded.evidence_json,validity='active',revision=excluded.revision,
                    updated_at=excluded.updated_at
                """,
                (identifier, project_id, category_id, from_node_id, to_node_id, relation,
                 1 if directed else 0, float(weight), confidence_value,
                 _json(evidence_value, {}), revision, now, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM project_graph_edges WHERE id=?", (identifier,)
            ).fetchone()
        result = dict(row)
        result["evidence"] = _decode(result.pop("evidence_json"), {})
        category = self._get_category(category_id)
        provider = self._provider_for(category or {})
        if provider is not None:
            provider.upsert_edge(project_id, category_id, result)
        return result

    def add_cross_link(
        self,
        project_id: str,
        from_node_id: str,
        to_node_id: str,
        relation_type: str,
        *,
        weight: float = 1.0,
        evidence: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        source, target = self.get_node(from_node_id), self.get_node(to_node_id)
        if source is None or target is None:
            raise GraphFederationError("cross-link endpoints must exist")
        if source["project_id"] != project_id or target["project_id"] != project_id:
            raise GraphFederationError("cross-link endpoints must belong to project")
        if source["category_id"] == target["category_id"]:
            raise GraphFederationError("same-category relation belongs in the category graph")
        identifier = _stable_id("crosslink", from_node_id, to_node_id, relation_type)
        now = time.time()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO project_graph_cross_links
                    (id,project_id,from_category_id,from_node_id,to_category_id,to_node_id,
                     relation_type,weight,evidence_json,validity,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)
                ON CONFLICT(id) DO UPDATE SET weight=excluded.weight,
                    evidence_json=excluded.evidence_json,validity='active',updated_at=excluded.updated_at
                """,
                (identifier, project_id, source["category_id"], from_node_id,
                 target["category_id"], to_node_id, str(relation_type), float(weight),
                 _json(evidence or {}, {}), now, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM project_graph_cross_links WHERE id=?", (identifier,)
            ).fetchone()
        result = dict(row)
        result["evidence"] = _decode(result.pop("evidence_json"), {})
        return result
    def delete_node(self, node_id: str) -> bool:
        """Delete one node and all derived edges/index rows without partial remote success."""
        node = self.get_node(node_id)
        if node is None:
            return False
        category = self._get_category(str(node["category_id"]))
        if category is None:
            return False
        provider = self._provider_for(category)
        if provider is None:
            self.rag.remove_document(
                self._category_namespace(str(node["project_id"]), str(node["category_id"])),
                node_id,
            )
        else:
            delete_node = getattr(provider, "delete_node", None)
            if not callable(delete_node):
                raise GraphFederationError(
                    "remote category provider must implement delete_node for asset-safe deletion"
                )
            delete_node(str(node["project_id"]), str(node["category_id"]), node_id)
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM project_graph_nodes WHERE id=?", (node_id,))
            connection.commit()
        return True


    def traverse(
        self,
        category_id: str,
        seed_node_ids: Iterable[str],
        *,
        depth: int = 2,
        relation_types: Optional[Iterable[str]] = None,
        direction: str = "both",
        max_nodes: int = 50,
        max_edges: int = 100,
    ) -> dict[str, Any]:
        max_depth = max(0, min(5, int(depth)))
        node_limit = max(1, min(500, int(max_nodes)))
        edge_limit = max(0, min(2000, int(max_edges)))
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be out, in, or both")
        selected_relations = {str(item) for item in (relation_types or [])}
        seeds = [str(item) for item in seed_node_ids]
        queue = deque((seed, 0) for seed in seeds)
        visited: set[str] = set()
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}
        paths: dict[str, list[str]] = {seed: [] for seed in seeds}
        truncated = False
        with closing(self.connect()) as connection:
            while queue:
                node_id, level = queue.popleft()
                if node_id in visited:
                    continue
                node = connection.execute(
                    "SELECT * FROM project_graph_nodes WHERE id=? AND category_id=? AND validity='active'",
                    (node_id, category_id),
                ).fetchone()
                if node is None:
                    continue
                visited.add(node_id)
                item = dict(node)
                item["provenance"] = _decode(item.pop("provenance_json"), {})
                nodes[node_id] = item
                if len(nodes) >= node_limit:
                    truncated = True
                    break
                if level >= max_depth:
                    continue
                if edge_limit == 0:
                    truncated = True
                    continue
                edge_rows = connection.execute(
                    """
                    SELECT * FROM project_graph_edges
                    WHERE category_id=? AND validity='active'
                      AND (from_node_id=? OR to_node_id=?)
                    ORDER BY id
                    """,
                    (category_id, node_id, node_id),
                ).fetchall()
                for edge_row in edge_rows:
                    edge = dict(edge_row)
                    if selected_relations and edge["relation_type"] not in selected_relations:
                        continue
                    if direction == "out" and edge["from_node_id"] != node_id:
                        continue
                    if direction == "in" and edge["to_node_id"] != node_id:
                        continue
                    if edge["directed"] and direction == "out" and edge["from_node_id"] != node_id:
                        continue
                    edge["evidence"] = _decode(edge.pop("evidence_json"), {})
                    edges[edge["id"]] = edge
                    if len(edges) >= edge_limit:
                        truncated = True
                        break
                    neighbor = edge["to_node_id"] if edge["from_node_id"] == node_id else edge["from_node_id"]
                    if neighbor not in paths:
                        paths[neighbor] = paths.get(node_id, []) + [edge["id"]]
                    if neighbor not in visited:
                        queue.append((neighbor, level + 1))
                if truncated:
                    break
        return {"nodes": list(nodes.values()), "edges": list(edges.values()),
                "paths": paths, "truncated": truncated}

    def _node_for_rag_hit(self, hit: Any) -> Optional[dict[str, Any]]:
        with closing(self.rag.connect()) as connection:
            row = connection.execute(
                "SELECT document_key FROM project_rag_documents WHERE id=?", (hit.document_id,)
            ).fetchone()
        return self.get_node(str(row["document_key"])) if row else None

    def search_category(
        self, project_id: str, category_id: str, query: str, *, top_k: int = 8,
        graph_depth: int = 1, filters: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        category = self._get_category(category_id)
        if category is None or category["project_id"] != project_id:
            raise GraphFederationError("category does not belong to project")
        provider = self._provider_for(category)
        if provider is not None:
            provider_hits = provider.search_category(
                project_id, category_id, query, top_k=top_k,
                graph_depth=graph_depth, filters=filters or {},
            )
            for hit in provider_hits:
                if str(hit.get("category_id")) != str(category_id):
                    raise GraphFederationError("provider returned a cross-category graph hit")
                hit.setdefault("category_revision", category["revision"])
                hit.setdefault("profile_id", str(category["backend_id"]))
                hit.setdefault("path_edge_ids", [])
            return provider_hits[: max(1, min(20, int(top_k)))]
        filters = filters or {}
        seed_hits = self.rag.search(
            self._category_namespace(project_id, category_id), query,
            kinds=filters.get("node_types"), source_ids=filters.get("source_ids"),
            top_k=max(1, min(20, int(top_k))), per_source_limit=3,
        )
        results: dict[str, dict[str, Any]] = {}
        for seed_hit in seed_hits:
            seed = self._node_for_rag_hit(seed_hit)
            if seed is None or seed["validity"] != "active":
                continue
            graph = self.traverse(
                category_id, [seed["id"]], depth=graph_depth,
                relation_types=filters.get("relation_types"), max_nodes=50, max_edges=100,
            )
            for node in graph["nodes"]:
                path = graph["paths"].get(node["id"], [])
                score = float(seed_hit.score) * (0.82 ** len(path))
                current = results.get(node["id"])
                if current is None or score > current["score"]:
                    results[node["id"]] = {
                        "category_id": category_id, "category_revision": category["revision"],
                        "node": node, "score": score, "seed_node_id": seed["id"],
                        "path_edge_ids": path, "profile_id": self.rag.profile.profile_id,
                    }
        return sorted(results.values(), key=lambda item: (-item["score"], item["node"]["id"]))[:top_k]

    def plan_query(
        self, project_id: str, query: str, *, top_categories: int = 3,
        per_category_top_k: int = 8, graph_depth: int = 1,
    ) -> dict[str, Any]:
        routes = self.classify(project_id, query, top_k=top_categories)
        return {
            "project_id": project_id, "query": query,
            "routes": routes,
            "queries": [{
                "category_id": route["category"]["id"], "query": query,
                "top_k": max(1, min(20, int(per_category_top_k))),
                "graph_depth": max(0, min(5, int(graph_depth))),
                "category_revision": route["category"]["revision"],
                "backend_id": route["category"]["backend_id"],
            } for route in routes],
        }

    def search(
        self, project_id: str, query: str, *, top_categories: int = 3,
        per_category_top_k: int = 8, total_limit: int = 20,
        graph_depth: int = 1, per_category_limit: int = 6,
    ) -> dict[str, Any]:
        plan = self.plan_query(
            project_id, query, top_categories=top_categories,
            per_category_top_k=per_category_top_k, graph_depth=graph_depth,
        )
        failures: list[dict[str, Any]] = []
        ranked: list[tuple[float, dict[str, Any]]] = []
        route_scores = {route["category"]["id"]: float(route["score"]) for route in plan["routes"]}
        for category_query in plan["queries"]:
            category_id = category_query["category_id"]
            try:
                hits = self.search_category(
                    project_id, category_id, query,
                    top_k=category_query["top_k"], graph_depth=category_query["graph_depth"],
                )
                for rank, hit in enumerate(hits, 1):
                    fused = (1.0 / (60.0 + rank)) * (0.5 + route_scores.get(category_id, 0.0))
                    ranked.append((fused, hit))
            except Exception as exc:
                failures.append({"category_id": category_id, "error": str(exc)})
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["node"]["id"]))
        category_counts: dict[str, int] = {}
        seen_nodes: set[str] = set()
        results: list[dict[str, Any]] = []
        for fused, hit in ranked:
            category_id = hit["category_id"]
            if hit["node"]["id"] in seen_nodes:
                continue
            if category_counts.get(category_id, 0) >= max(1, int(per_category_limit)):
                continue
            category_counts[category_id] = category_counts.get(category_id, 0) + 1
            seen_nodes.add(hit["node"]["id"])
            hit = dict(hit)
            hit["federated_score"] = fused
            results.append(hit)
            if len(results) >= max(1, min(100, int(total_limit))):
                break
        return {"plan": plan, "hits": results, "failures": failures,
                "partial": bool(failures), "unrouted": not plan["routes"]}

    def cross_links(
        self, project_id: str, node_ids: Iterable[str], *, limit: int = 20
    ) -> list[dict[str, Any]]:
        selected = [str(node_id) for node_id in node_ids if node_id][:100]
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        params: list[Any] = [project_id, *selected, *selected, max(1, min(100, int(limit)))]
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM project_graph_cross_links
                WHERE project_id=? AND validity='active'
                  AND (from_node_id IN ({placeholders}) OR to_node_id IN ({placeholders}))
                ORDER BY weight DESC,id LIMIT ?
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _decode(item.pop("evidence_json"), {})
            result.append(item)
        return result

    def delete_category(
        self, category_id: str, *, cascade: bool = False,
        replacement_inbox_id: Optional[str] = None,
    ) -> dict[str, int]:
        category = self._get_category(category_id)
        if category is None:
            return {"categories": 0, "nodes": 0}
        with closing(self.connect()) as connection:
            children = connection.execute(
                "SELECT id FROM project_graph_categories WHERE parent_id=? AND status='active'",
                (category_id,),
            ).fetchall()
            node_count = int(connection.execute(
                "SELECT COUNT(*) FROM project_graph_nodes WHERE category_id=?", (category_id,)
            ).fetchone()[0])
        if (children or node_count) and not cascade:
            raise GraphFederationError(
                "non-empty category deletion requires cascade or a separate migration"
            )
        if category.get("is_inbox"):
            replacement = self._get_category(str(replacement_inbox_id)) if replacement_inbox_id else None
            if replacement is None or replacement["project_id"] != category["project_id"]:
                raise GraphFederationError("deleting the inbox requires a replacement inbox category")
        descendants: list[dict[str, Any]] = []
        queue = deque([category_id])
        while queue:
            current = queue.popleft()
            current_category = self._get_category(current)
            if current_category is None:
                continue
            descendants.append(current_category)
            with closing(self.connect()) as connection:
                child_rows = connection.execute(
                    "SELECT id FROM project_graph_categories WHERE parent_id=?", (current,)
                ).fetchall()
            queue.extend(str(row["id"]) for row in child_rows)
        descendant_ids = {str(item["id"]) for item in descendants}
        if replacement_inbox_id and str(replacement_inbox_id) in descendant_ids:
            raise GraphFederationError("replacement inbox cannot be inside the deleted subtree")
        total_nodes = 0
        for item in descendants:
            provider = self._provider_for(item)
            if provider is not None:
                provider.delete_category(str(category["project_id"]), str(item["id"]))
        with closing(self.connect()) as connection:
            for item in reversed(descendants):
                total_nodes += int(connection.execute(
                    "SELECT COUNT(*) FROM project_graph_nodes WHERE category_id=?", (item["id"],)
                ).fetchone()[0])
                connection.execute(
                    "DELETE FROM project_graph_categories WHERE id=?", (item["id"],)
                )
            if replacement_inbox_id:
                connection.execute(
                    "UPDATE project_graph_categories SET is_inbox=1,revision=revision+1,updated_at=? WHERE id=?",
                    (time.time(), replacement_inbox_id),
                )
            connection.commit()
        route_namespace = self._route_namespace(str(category["project_id"]))
        for item in descendants:
            self.rag.remove_document(route_namespace, str(item["id"]))
            self.rag.delete_project(
                self._category_namespace(str(category["project_id"]), str(item["id"]))
            )
        return {"categories": len(descendants), "nodes": total_nodes}

    def delete_project(self, project_id: str) -> dict[str, int]:
        categories = self.list_categories(project_id)
        for category in categories:
            provider = self._provider_for(category)
            if provider is not None:
                provider.delete_category(project_id, str(category["id"]))
        with closing(self.connect()) as connection:
            node_count = connection.execute(
                "SELECT COUNT(*) FROM project_graph_nodes WHERE project_id=?", (project_id,)
            ).fetchone()[0]
            connection.execute("DELETE FROM project_graph_cross_links WHERE project_id=?", (project_id,))
            connection.execute(
                "UPDATE project_graph_categories SET parent_id=NULL WHERE project_id=?", (project_id,)
            )
            connection.execute("DELETE FROM project_graph_categories WHERE project_id=?", (project_id,))
            connection.commit()
        self.rag.delete_project(self._route_namespace(project_id))
        for category in categories:
            self.rag.delete_project(self._category_namespace(project_id, category["id"]))
        return {"categories": len(categories), "nodes": int(node_count)}
