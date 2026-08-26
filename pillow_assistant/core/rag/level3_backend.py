"""Level-3 federated GraphRAG project-memory composition."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from pillow_assistant.core.project_memory_backend import (
    BackendCapabilities,
    infer_backend_capabilities,
    validate_backend_capabilities,
)
from pillow_assistant.core.rag.graph_federation import FederatedGraphRAG
from pillow_assistant.core.rag.multimodal import MultimodalAssetStore


class Level3ProjectMemoryBackend:
    capabilities = BackendCapabilities(
        backend_id="level3-federated-graphrag", authoritative_state=True,
        task_validation=True, resume=True, source_references=True,
        keyword_search=True, vector_search=True, metadata_filter=True, delete_project=True,
    )

    def __init__(
        self, structured_backend: Any, federation: FederatedGraphRAG, *,
        mode: str = "augment", fallback_backend: Any = None,
        category_templates: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.structured_backend = structured_backend
        self.federation = federation
        self.assets = MultimodalAssetStore(federation.db_path, federation)
        self.assets.ensure_schema()
        self.mode = str(mode or "augment").lower()
        self.fallback_backend = fallback_backend or structured_backend
        self.category_templates = list(category_templates or [])
        if self.mode == "replace":
            control = infer_backend_capabilities(structured_backend, "level3-control")
            combined = BackendCapabilities(
                backend_id=self.capabilities.backend_id,
                authoritative_state=control.authoritative_state,
                task_validation=control.task_validation, resume=control.resume,
                source_references=control.source_references, delete_project=control.delete_project,
                keyword_search=True, vector_search=True, metadata_filter=True,
            )
            validate_backend_capabilities(combined, "replace")
        elif self.mode != "augment":
            raise ValueError("Level3ProjectMemoryBackend mode must be augment or replace")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.structured_backend, name)

    def ensure_project(self, project_id: str, goal: str = "") -> dict[str, Any]:
        state = self.structured_backend.ensure_project(project_id, goal)
        known: dict[str, str] = {}
        pending = list(self.category_templates)
        for _ in range(len(pending) * 2 + 1):
            if not pending:
                break
            raw = pending.pop(0)
            parent_name = raw.get("parent")
            if parent_name and str(parent_name) not in known:
                pending.append(raw)
                continue
            category = self.federation.register_category(
                project_id, str(raw.get("name") or ""),
                description=str(raw.get("description") or ""),
                parent_id=known.get(str(parent_name)) if parent_name else None,
                routing_examples=raw.get("routing_examples"),
                backend_id=str(raw.get("backend_id") or "local"),
                modalities=raw.get("modalities"), is_inbox=bool(raw.get("is_inbox")),
            )
            known[str(raw.get("name"))] = str(category["id"])
        return state

    def add_memory_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        item = self.structured_backend.add_memory_item(*args, **kwargs)
        result = dict(item)
        routes = self.federation.resolve_category(
            str(item["project_id"]), str(item.get("content") or ""),
            subject_type="memory_item", subject_id=str(item["id"]), top_k=1,
        )
        if not routes:
            result["graph_index_status"] = "unrouted"
            return result
        category = routes[0]["category"]
        try:
            node = self.federation.upsert_node(
                str(item["project_id"]), str(category["id"]), str(item["id"]),
                node_type=str(item.get("kind") or "fact"),
                label=str(item.get("content") or "")[:120] or str(item["id"]),
                content=str(item.get("content") or ""), task_id=item.get("task_id"),
                turn_id=item.get("source_turn_id"),
                provenance={"memory_item_id": item["id"], "route_score": routes[0]["score"]},
            )
            result["graph_index_status"] = "published"
            result["graph_node_id"] = node["id"]
            result["category_id"] = category["id"]
        except Exception as exc:
            result["graph_index_status"] = "failed"
            result["graph_index_error"] = str(exc)
        return result

    def search_memory(
        self, project_id: str, query: str, *, kinds: Optional[Iterable[str]] = None,
        task_id: Optional[str] = None, limit: int = 8,
    ) -> list[dict[str, Any]]:
        try:
            federated = self.federation.search(project_id, query, total_limit=limit)
            if federated["hits"]:
                results = []
                for hit in federated["hits"]:
                    node = hit["node"]
                    if kinds and node["node_type"] not in set(kinds):
                        continue
                    if task_id is not None and node.get("task_id") != task_id:
                        continue
                    results.append({
                        "id": node["id"], "source_id": node.get("source_id") or node["id"],
                        "source_type": "graph_rag", "task_id": node.get("task_id"),
                        "kind": node["node_type"], "content": node.get("content") or node["label"],
                        "score": hit["federated_score"], "category_id": hit["category_id"],
                        "category_revision": hit["category_revision"],
                        "path_edge_ids": hit["path_edge_ids"], "profile_id": hit["profile_id"],
                        "partial": federated["partial"],
                    })
                if results:
                    return results[:limit]
        except Exception:
            if self.mode == "replace":
                raise
        if hasattr(self.fallback_backend, "search_memory"):
            return self.fallback_backend.search_memory(
                project_id, query, kinds=kinds, task_id=task_id, limit=limit
            )
        return []

    def delete_project_memory(self, project_id: str) -> None:
        self.assets.delete_project(project_id)
        self.federation.delete_project(project_id)
        self.structured_backend.delete_project_memory(project_id)


def build_level3_backend(
    lower_backend: Any, db_path: Any, config: Optional[dict[str, Any]] = None
) -> Any:
    settings = dict(config or {})
    mode = str(settings.get("mode") or "disabled").lower()
    if mode == "disabled":
        return lower_backend
    provider = str(settings.get("provider") or "local").lower()
    if provider != "local":
        raise ValueError(
            f"GraphRAG provider '{provider}' requires an installed adapter; refusing silent fallback"
        )
    federation = FederatedGraphRAG(db_path)
    federation.ensure_schema()
    if not federation.health().get("ok"):
        raise RuntimeError("local Federated GraphRAG health check failed")
    return Level3ProjectMemoryBackend(
        lower_backend, federation, mode=mode, fallback_backend=lower_backend,
        category_templates=[raw for raw in settings.get("categories") or []
                            if isinstance(raw, dict) and raw.get("name")],
    )
