"""Layer-2 project-memory backend composition."""

from __future__ import annotations

from contextlib import closing
from typing import Any, Iterable, Optional

from pillow_assistant.core.rag.local_hybrid import LocalHybridRAG, StaleIndexJob
from pillow_assistant.core.project_memory_backend import (
    BackendCapabilities,
    infer_backend_capabilities,
    validate_backend_capabilities,
)


class Layer2ProjectMemoryBackend:
    capabilities = BackendCapabilities(
        backend_id="layer2-hybrid-project-memory", authoritative_state=True,
        task_validation=True, resume=True, source_references=True,
        keyword_search=True, vector_search=True, metadata_filter=True, delete_project=True,
    )

    def __init__(
        self, structured_backend: Any, rag: Any, *, mode: str = "augment",
        fallback_to_structured: bool = True,
    ) -> None:
        self.structured_backend = structured_backend
        self.rag = rag
        self.mode = str(mode or "augment").lower()
        self.fallback_to_structured = bool(fallback_to_structured)
        validate_backend_capabilities(infer_backend_capabilities(rag, "rag"), "augment")
        if self.mode == "replace":
            control = infer_backend_capabilities(structured_backend, "structured")
            combined = BackendCapabilities(
                backend_id=self.capabilities.backend_id,
                authoritative_state=control.authoritative_state,
                task_validation=control.task_validation, resume=control.resume,
                source_references=control.source_references,
                keyword_search=True, vector_search=True, metadata_filter=True,
                delete_project=control.delete_project,
            )
            validate_backend_capabilities(combined, "replace")
        elif self.mode != "augment":
            raise ValueError("Layer2ProjectMemoryBackend mode must be augment or replace")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.structured_backend, name)

    def add_memory_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        item = self.structured_backend.add_memory_item(*args, **kwargs)
        result = dict(item)
        try:
            self.rag.index_internal(
                str(item["project_id"]), str(item["id"]), str(item.get("content") or ""),
                kind=str(item.get("kind") or "fact"), task_id=item.get("task_id"),
            )
            result["index_status"] = "published"
        except Exception as exc:
            result["index_status"] = "failed"
            result["index_error"] = str(exc)
        return result

    def search_memory(
        self, project_id: str, query: str, *, kinds: Optional[Iterable[str]] = None,
        task_id: Optional[str] = None, source_ids: Optional[Iterable[str]] = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        try:
            hits = self.rag.search(
                project_id, query, kinds=kinds, task_id=task_id,
                source_ids=source_ids, top_k=limit,
            )
            if hits:
                return [{
                    "id": hit.chunk_id, "source_id": hit.source_id or hit.document_id,
                    "source_type": "rag", "task_id": hit.task_id, "kind": hit.kind,
                    "content": hit.content, "score": hit.score,
                    "score_breakdown": {
                        "lexical": hit.lexical_score, "vector": hit.vector_score,
                    },
                    "path": hit.path, "start_offset": hit.start_offset,
                    "end_offset": hit.end_offset, "fingerprint": hit.fingerprint,
                    "profile_id": hit.profile_id, "stale": hit.stale,
                } for hit in hits]
        except Exception:
            if not (self.mode == "augment" and self.fallback_to_structured):
                raise
        if self.fallback_to_structured and hasattr(self.structured_backend, "search_memory"):
            return self.structured_backend.search_memory(
                project_id, query, kinds=kinds, task_id=task_id, limit=limit
            )
        return []

    def register_source(self, project_id: str, path: Any, **kwargs: Any) -> dict[str, Any]:
        source = self.structured_backend.register_source(project_id, path, **kwargs)
        result = dict(source)
        try:
            job = self.rag.enqueue_source(project_id, source)
            result["index_status"] = "queued"
            result["index_job_id"] = job["job_id"]
        except Exception as exc:
            result["index_status"] = "failed"
            result["index_error"] = str(exc)
        return result

    def process_pending_jobs(self, project_id: Optional[str] = None, limit: int = 20) -> dict[str, int]:
        counts = {"processed": 0, "succeeded": 0, "failed": 0}
        for job in self.rag.pending_jobs(project_id, limit):
            counts["processed"] += 1
            try:
                with closing(self.rag.connect()) as connection:
                    document = connection.execute(
                        "SELECT source_id,project_id FROM project_rag_documents WHERE id=?",
                        (job["document_id"],),
                    ).fetchone()
                if document is None or not document["source_id"]:
                    raise ValueError("queued source document is missing")
                source = self.structured_backend.get_source(str(document["source_id"]))
                if source is None:
                    raise ValueError("queued source reference is missing")
                self.rag.index_source(
                    str(document["project_id"]), source,
                    expected_queue_fingerprint=str(job["fingerprint"]),
                )
                self.rag.finish_job(job["idempotency_key"], ok=True)
                counts["succeeded"] += 1
            except StaleIndexJob:
                self.rag.supersede_job(job["idempotency_key"])
            except Exception as exc:
                self.rag.finish_job(job["idempotency_key"], ok=False, error=str(exc))
                counts["failed"] += 1
        return counts

    def refresh_source(self, source_id: str, **kwargs: Any) -> dict[str, Any]:
        source = self.structured_backend.refresh_source(source_id, **kwargs)
        result = dict(source)
        if source.get("availability") in {"changed", "available"}:
            try:
                job = self.rag.enqueue_source(str(source["project_id"]), source)
                result["index_status"] = "queued"
                result["index_job_id"] = job["job_id"]
            except Exception as exc:
                result["index_status"] = "failed"
                result["index_error"] = str(exc)
        return result

    def delete_project_memory(self, project_id: str) -> None:
        self.rag.delete_project(project_id)
        self.structured_backend.delete_project_memory(project_id)


def build_layer2_backend(
    structured_backend: Any, db_path: Any, config: Optional[dict[str, Any]] = None
) -> Any:
    """Build the optional runtime backend; disabled preserves Level-1 exactly."""
    settings = dict(config or {})
    mode = str(settings.get("mode") or "disabled").lower()
    if mode == "disabled":
        return structured_backend
    provider = str(settings.get("provider") or "local").lower()
    if provider != "local":
        raise ValueError(
            f"RAG provider '{provider}' requires an installed adapter; refusing silent fallback"
        )
    rag = LocalHybridRAG(
        db_path,
        chunk_chars=int(settings.get("chunk_chars") or 1600),
        overlap_chars=int(settings.get("overlap_chars") or 160),
        max_file_chars=int(settings.get("max_file_chars") or 5_000_000),
        candidate_limit=int(settings.get("candidate_limit") or 2000),
    )
    rag.ensure_schema()
    health = rag.health()
    if not health.get("ok"):
        raise RuntimeError(f"local Hybrid RAG health check failed: {health.get('error', 'unknown')}")
    return Layer2ProjectMemoryBackend(
        structured_backend, rag, mode=mode,
        fallback_to_structured=bool(settings.get("fallback_to_structured", True)),
    )
