from __future__ import annotations

import math
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pillow_assistant.core.project_memory_backend import (
    BackendCapabilities,
    BackendCapabilityError,
    validate_backend_capabilities,
)
from pillow_assistant.core.rag.base import (
    ContentUploadDenied,
    EmbeddingProfileMismatch,
    LocalHashEmbeddingProvider,
    UploadPolicy,
    guard_remote_upload,
)
from pillow_assistant.core.rag.local_hybrid import LocalHybridRAG, StaleIndexJob, chunk_text
from pillow_assistant.core.rag.project_backend import Layer2ProjectMemoryBackend
from storage.project_memory import ProjectMemoryStore
from storage.projects import ProjectStore


class SemanticFixtureEmbedding:
    provider_id = "fixture"
    model_id = "semantic-v1"
    dimension = 3
    is_remote = False

    def embed(self, texts):
        result = []
        for text in texts:
            value = text.lower()
            if any(word in value for word in ("automobile", "vehicle", "car")):
                result.append([1.0, 0.0, 0.0])
            elif "ocean" in value:
                result.append([0.0, 1.0, 0.0])
            else:
                result.append([0.0, 0.0, 1.0])
        return result

    def health(self):
        return {"ok": True}


class RemoteFixtureEmbedding(SemanticFixtureEmbedding):
    provider_id = "remote-fixture"
    is_remote = True


class Layer2RAGTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_store = ProjectStore(self.root / "projects")
        self.project = self.project_store.create("rag")
        self.structured = ProjectMemoryStore(self.root / "assistant.db", self.project_store.base)
        self.structured.ensure_schema()
        self.structured.ensure_project(self.project.id)
        self.rag = LocalHybridRAG(
            self.root / "assistant.db", embedding=SemanticFixtureEmbedding(),
            chunk_chars=256, overlap_chars=32,
        )
        self.rag.ensure_schema()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capability_validation_for_augment_and_replace(self) -> None:
        retrieval = BackendCapabilities(
            backend_id="r", keyword_search=True, vector_search=True, metadata_filter=True
        )
        validate_backend_capabilities(retrieval, "augment")
        with self.assertRaises(BackendCapabilityError):
            validate_backend_capabilities(retrieval, "replace")
        complete = BackendCapabilities(
            backend_id="full", authoritative_state=True, task_validation=True, resume=True,
            source_references=True, keyword_search=True, vector_search=True,
            metadata_filter=True, delete_project=True,
        )
        validate_backend_capabilities(complete, "replace")

    def test_hash_embedding_is_deterministic_normalized_and_dimensioned(self) -> None:
        provider = LocalHashEmbeddingProvider(64)
        first, second = provider.embed(["SQLite 项目记忆", "SQLite 项目记忆"])
        self.assertEqual(first, second)
        self.assertEqual(64, len(first))
        self.assertAlmostEqual(1.0, math.sqrt(sum(value * value for value in first)), places=6)

    def test_chunking_is_stable_covering_and_bounded(self) -> None:
        text = ("section one\n" * 40) + "\n\n" + ("第二部分。" * 80)
        one = chunk_text(text, chunk_chars=256, overlap_chars=32)
        two = chunk_text(text, chunk_chars=256, overlap_chars=32)
        self.assertEqual(one, two)
        self.assertEqual(0, one[0].start_offset)
        self.assertEqual(len(text), one[-1].end_offset)
        self.assertTrue(all(0 < item.end_offset - item.start_offset <= 256 for item in one))
        self.assertTrue(all(one[index + 1].start_offset <= one[index].end_offset
                            for index in range(len(one) - 1)))

    def test_internal_index_is_idempotent_and_new_generation_is_atomic(self) -> None:
        self.rag.index_internal(self.project.id, "memory-1", "SQLite authoritative", kind="decision")
        self.rag.index_internal(self.project.id, "memory-1", "SQLite authoritative", kind="decision")
        with closing(self.rag.connect()) as connection:
            document = connection.execute(
                "SELECT * FROM project_rag_documents WHERE project_id=?", (self.project.id,)
            ).fetchone()
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM project_rag_generations WHERE document_id=?", (document["id"],)
            ).fetchone()[0])
        self.rag.index_internal(self.project.id, "memory-1", "SQLite and vectors", kind="decision")
        with closing(self.rag.connect()) as connection:
            statuses = [row["status"] for row in connection.execute(
                "SELECT status FROM project_rag_generations WHERE document_id=? ORDER BY generation_no",
                (document["id"],),
            ).fetchall()]
        self.assertEqual(["superseded", "published"], statuses)

    def test_external_file_stores_offsets_and_vectors_not_body(self) -> None:
        path = self.root / "external.md"
        body = "secret external body about automobiles\n" * 20
        path.write_text(body, encoding="utf-8")
        source = self.structured.register_source(self.project.id, path)
        result = self.rag.index_source(self.project.id, source)
        self.assertEqual("published", result["status"])
        with closing(self.rag.connect()) as connection:
            chunks = connection.execute(
                "SELECT internal_text,start_offset,end_offset FROM project_rag_chunks WHERE source_id=?",
                (source["id"],),
            ).fetchall()
        self.assertTrue(chunks)
        self.assertTrue(all(row["internal_text"] is None for row in chunks))
        self.assertTrue(all(row["end_offset"] > row["start_offset"] for row in chunks))

    def test_bm25_and_vector_paths_both_recall(self) -> None:
        self.rag.index_internal(
            self.project.id, "lexical", "rarekeyword database migration", kind="fact"
        )
        self.rag.index_internal(
            self.project.id, "semantic", "automobile maintenance manual", kind="fact"
        )
        lexical = self.rag.search(
            self.project.id, "rarekeyword", top_k=3, lexical_weight=1, vector_weight=0
        )
        vector = self.rag.search(
            self.project.id, "vehicle", top_k=3, lexical_weight=0, vector_weight=1
        )
        self.assertIn("rarekeyword", lexical[0].content)
        self.assertIn("automobile", vector[0].content)

    def test_project_task_kind_and_source_filters_do_not_leak(self) -> None:
        other = self.project_store.create("other")
        self.rag.index_internal(self.project.id, "a", "automobile one", kind="decision", task_id="t1")
        self.rag.index_internal(self.project.id, "b", "automobile two", kind="fact", task_id="t2")
        self.rag.index_internal(other.id, "c", "automobile other project", kind="decision", task_id="t1")
        hits = self.rag.search(
            self.project.id, "vehicle", task_id="t1", kinds=["decision"], top_k=10
        )
        self.assertEqual(1, len(hits))
        self.assertEqual("t1", hits[0].task_id)
        self.assertNotIn("other project", hits[0].content)

    def test_per_source_limit_preserves_diversity(self) -> None:
        long_text = ("automobile paragraph. " * 50) + "\n\n" + ("automobile section. " * 50)
        self.rag.index_internal(self.project.id, "long", long_text, kind="fact")
        self.rag.index_internal(self.project.id, "short", "automobile short", kind="fact")
        hits = self.rag.search(self.project.id, "vehicle", top_k=4, per_source_limit=1)
        self.assertEqual(2, len(hits))
        self.assertEqual(2, len({hit.document_id for hit in hits}))

    def test_external_hit_is_materialized_and_becomes_stale_after_change(self) -> None:
        path = self.root / "source.txt"
        path.write_text("automobile source content", encoding="utf-8")
        source = self.structured.register_source(self.project.id, path)
        self.rag.index_source(self.project.id, source)
        fresh = self.rag.search(self.project.id, "vehicle", top_k=1)
        self.assertEqual("automobile source content", fresh[0].content)
        self.assertFalse(fresh[0].stale)
        path.write_text("changed ocean content", encoding="utf-8")
        stale = self.rag.search(self.project.id, "vehicle", top_k=1)
        self.assertTrue(stale[0].stale)
        self.assertEqual("", stale[0].content)

    def test_profile_isolation_and_dimension_mismatch(self) -> None:
        other_rag = LocalHybridRAG(
            self.root / "assistant.db", embedding=LocalHashEmbeddingProvider(32),
            chunk_chars=256, overlap_chars=32,
        )
        other_rag.ensure_schema()
        self.rag.index_internal(self.project.id, "fixture", "automobile", kind="fact")
        other_rag.index_internal(self.project.id, "hash", "SQLite", kind="fact")
        self.assertNotEqual(self.rag.profile.profile_id, other_rag.profile.profile_id)
        self.assertTrue(self.rag.search(self.project.id, "vehicle"))
        self.assertTrue(other_rag.search(self.project.id, "SQLite"))
        with self.assertRaises(EmbeddingProfileMismatch):
            other_rag._cosine([1.0], [1.0])

    def test_layer2_backend_indexes_internal_memory_and_falls_back(self) -> None:
        backend = Layer2ProjectMemoryBackend(self.structured, self.rag, mode="augment")
        item = backend.add_memory_item(self.project.id, "decision", "automobile storage")
        self.assertEqual("published", item["index_status"])
        self.assertTrue(backend.search_memory(self.project.id, "vehicle"))

        class BrokenRAG:
            capabilities = self.rag.capabilities
            def search(self, *args, **kwargs):
                raise RuntimeError("offline")

        fallback = Layer2ProjectMemoryBackend(self.structured, BrokenRAG(), mode="augment")
        hits = fallback.search_memory(self.project.id, "automobile")
        self.assertTrue(hits)
        self.assertEqual("decision", hits[0]["kind"])

    def test_replace_mode_rejects_incomplete_control_backend(self) -> None:
        class Incomplete:
            pass
        with self.assertRaises(BackendCapabilityError):
            Layer2ProjectMemoryBackend(Incomplete(), self.rag, mode="replace")

    def test_source_jobs_are_idempotent_and_processable(self) -> None:
        path = self.root / "queued.md"
        path.write_text("queued automobile", encoding="utf-8")
        backend = Layer2ProjectMemoryBackend(self.structured, self.rag)
        first = backend.register_source(self.project.id, path)
        second = backend.register_source(self.project.id, path)
        self.assertEqual(first["index_job_id"], second["index_job_id"])
        counts = backend.process_pending_jobs(self.project.id)
        self.assertEqual({"processed": 1, "succeeded": 1, "failed": 0}, counts)
        self.assertTrue(backend.search_memory(self.project.id, "vehicle"))

    def test_deleted_document_tombstone_blocks_a_worker_that_already_read_the_job(self) -> None:
        path = self.root / "deleted-race.md"
        path.write_text("automobile before delete", encoding="utf-8")
        source = self.structured.register_source(self.project.id, path)
        job = self.rag.enqueue_source(self.project.id, source)
        self.assertEqual(1, self.rag.remove_document(self.project.id, source["id"]))
        with self.assertRaises(StaleIndexJob):
            self.rag.index_source(
                self.project.id, source,
                expected_queue_fingerprint=str(source.get("content_hash") or f"{source.get('size')}:{source.get('mtime_ns')}"),
            )
        self.rag.finish_job(job["job_id"], ok=False, error="late worker")
        with closing(self.rag.connect()) as connection:
            document = connection.execute(
                "SELECT status FROM project_rag_documents WHERE id=?", (job["document_id"],)
            ).fetchone()
            status = connection.execute(
                "SELECT status FROM project_rag_jobs WHERE idempotency_key=?", (job["job_id"],)
            ).fetchone()["status"]
        self.assertEqual("deleted", document["status"])
        self.assertEqual("deleted", status)

    def test_older_fingerprint_job_is_superseded_before_new_generation_publishes(self) -> None:
        path = self.root / "changed-queue.md"
        path.write_text("first automobile", encoding="utf-8")
        backend = Layer2ProjectMemoryBackend(self.structured, self.rag)
        first = backend.register_source(self.project.id, path)
        source_id = first["id"]
        path.write_text("second version automobile with changed size", encoding="utf-8")
        refreshed = backend.refresh_source(source_id)
        self.assertEqual("queued", refreshed["index_status"])
        self.assertNotEqual(
            first["index_job_id"], refreshed["index_job_id"]
        )

        counts = backend.process_pending_jobs(self.project.id)
        self.assertEqual({"processed": 2, "succeeded": 1, "failed": 0}, counts)
        with closing(self.rag.connect()) as connection:
            rows = connection.execute(
                """
                SELECT idempotency_key,status FROM project_rag_jobs
                WHERE project_id=? ORDER BY created_at
                """,
                (self.project.id,),
            ).fetchall()
        statuses = {row["idempotency_key"]: row["status"] for row in rows}
        self.assertEqual("superseded", statuses[first["index_job_id"]])
    def test_remote_upload_requires_matching_explicit_approval(self) -> None:
        provider = RemoteFixtureEmbedding()
        denied = UploadPolicy(provider_id=provider.provider_id, project_id=self.project.id)
        with self.assertRaises(ContentUploadDenied):
            guard_remote_upload(provider, self.project.id, denied)
        approved = UploadPolicy(
            provider_id=provider.provider_id, project_id=self.project.id,
            allow_content_upload=True, approved_at=1.0,
        )
        guard_remote_upload(provider, self.project.id, approved)

    def test_delete_project_removes_all_derived_index_data(self) -> None:
        self.rag.index_internal(self.project.id, "one", "automobile", kind="fact")
        self.assertEqual(1, self.rag.delete_project(self.project.id))
        self.assertEqual([], self.rag.search(self.project.id, "vehicle"))
        with closing(self.rag.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM project_rag_chunks WHERE project_id=?", (self.project.id,)
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
