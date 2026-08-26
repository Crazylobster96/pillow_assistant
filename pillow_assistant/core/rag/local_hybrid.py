"""Dependency-free local BM25 + vector project-memory index."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

from pillow_assistant.core.project_memory_backend import BackendCapabilities
from pillow_assistant.core.rag.base import (
    EmbeddingError,
    EmbeddingProfile,
    EmbeddingProfileMismatch,
    LocalHashEmbeddingProvider,
    RAGError,
    RAGHit,
    TextChunk,
    tokenize,
)


SUPPORTED_TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".toml", ".csv",
}
PARSER_VERSION = "plain-text-v1"


class StaleIndexJob(RAGError):
    """A queued generation is older than a tombstone or replacement job."""


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part) for part in parts)
    return prefix + "_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def chunk_text(text: str, *, chunk_chars: int = 1600, overlap_chars: int = 160) -> list[TextChunk]:
    size = int(chunk_chars)
    overlap = int(overlap_chars)
    if size < 256 or size > 16_000:
        raise ValueError("chunk_chars must be between 256 and 16000")
    if overlap < 0 or overlap > size // 2:
        raise ValueError("overlap_chars must be between 0 and half the chunk size")
    value = str(text or "")
    if not value:
        return []
    result: list[TextChunk] = []
    start = 0
    length = len(value)
    while start < length:
        hard_end = min(length, start + size)
        end = hard_end
        if hard_end < length:
            floor = start + max(128, int(size * 0.58))
            candidates = [
                value.rfind("\n\n", floor, hard_end),
                value.rfind("\n#", floor, hard_end),
                value.rfind("\n", floor, hard_end),
                value.rfind("。", floor, hard_end),
                value.rfind(". ", floor, hard_end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + (2 if value[boundary : boundary + 2] in {"\n\n", ". "} else 1)
        if end <= start:
            end = hard_end
        body = value[start:end]
        if body:
            result.append(TextChunk(start, end, body))
        if end >= length:
            break
        next_start = end - overlap
        start = next_start if next_start > start else end
    return result


def read_text_source(path: str | Path, max_chars: int = 5_000_000) -> tuple[str, str]:
    candidate = Path(path)
    if candidate.suffix.lower() not in SUPPORTED_TEXT_EXTENSIONS:
        return "", "unsupported"
    try:
        if candidate.stat().st_size > max_chars * 4:
            return "", "too_large"
        raw = candidate.read_bytes()
    except FileNotFoundError:
        return "", "missing"
    except OSError:
        return "", "unreadable"
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            if len(text) > max_chars:
                return "", "too_large"
            return text, "empty" if not text else "ready"
        except UnicodeDecodeError:
            continue
    return "", "unreadable"


class LocalHybridRAG:
    capabilities = BackendCapabilities(
        backend_id="local-hybrid-rag", keyword_search=True, vector_search=True,
        metadata_filter=True,
    )

    def __init__(
        self,
        db_path: str | Path,
        embedding: Any = None,
        *,
        chunk_chars: int = 1600,
        overlap_chars: int = 160,
        max_file_chars: int = 5_000_000,
        candidate_limit: int = 2000,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding = embedding or LocalHashEmbeddingProvider()
        self.chunk_chars = int(chunk_chars)
        self.overlap_chars = int(overlap_chars)
        chunk_text("test", chunk_chars=self.chunk_chars, overlap_chars=self.overlap_chars)
        self.max_file_chars = max(1_000, int(max_file_chars))
        self.candidate_limit = max(10, min(100_000, int(candidate_limit)))
        self.profile = EmbeddingProfile(
            provider_id=str(self.embedding.provider_id), model_id=str(self.embedding.model_id),
            dimension=int(self.embedding.dimension), metric="cosine",
        )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def ensure_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS project_rag_documents (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_key TEXT NOT NULL,
                source_id TEXT, document_kind TEXT NOT NULL, original_path TEXT,
                normalized_path TEXT, fingerprint TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL, profile_id TEXT NOT NULL,
                active_generation TEXT, status TEXT NOT NULL, error_code TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(project_id, document_key, profile_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_rag_generations (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, generation_no INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, profile_id TEXT NOT NULL, status TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, published_at REAL,
                UNIQUE(document_id, generation_no),
                FOREIGN KEY(document_id) REFERENCES project_rag_documents(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_rag_chunks (
                id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, project_id TEXT NOT NULL,
                document_id TEXT NOT NULL, source_id TEXT, task_id TEXT, kind TEXT NOT NULL,
                start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, internal_text TEXT,
                term_freq_json TEXT NOT NULL, token_count INTEGER NOT NULL,
                embedding_json TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
                FOREIGN KEY(generation_id) REFERENCES project_rag_generations(id) ON DELETE CASCADE,
                FOREIGN KEY(document_id) REFERENCES project_rag_documents(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_rag_jobs (
                idempotency_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, profile_id TEXT NOT NULL, operation TEXT NOT NULL,
                status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
            """,
        )
        indexes = (
            "CREATE INDEX IF NOT EXISTS idx_rag_doc_active ON project_rag_documents(project_id,profile_id,status)",
            "CREATE INDEX IF NOT EXISTS idx_rag_gen_doc ON project_rag_generations(document_id,status)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunk_filter ON project_rag_chunks(project_id,task_id,kind,source_id)",
            "CREATE INDEX IF NOT EXISTS idx_rag_jobs_status ON project_rag_jobs(status,updated_at)",
        )
        with closing(self.connect()) as connection:
            for statement in statements:
                connection.execute(statement)
            for statement in indexes:
                connection.execute(statement)
            connection.commit()

    def health(self) -> dict[str, Any]:
        try:
            with closing(self.connect()) as connection:
                connection.execute("SELECT 1").fetchone()
            provider = self.embedding.health()
            return {"ok": bool(provider.get("ok", True)), "profile_id": self.profile.profile_id,
                    "provider": provider}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "profile_id": self.profile.profile_id}

    def _document_identity(
        self, project_id: str, document_key: str, document_kind: str
    ) -> str:
        return _stable_id("ragdoc", project_id, document_kind, document_key, self.profile.profile_id)

    def _set_document_status(
        self, project_id: str, document_key: str, document_kind: str, *, source_id: Optional[str],
        original_path: Optional[str], normalized_path: Optional[str], fingerprint: str,
        status: str, error_code: Optional[str],
    ) -> dict[str, Any]:
        identifier = self._document_identity(project_id, document_key, document_kind)
        now = time.time()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO project_rag_documents
                    (id,project_id,document_key,source_id,document_kind,original_path,
                     normalized_path,fingerprint,parser_version,profile_id,status,error_code,
                     created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?,?)
                ON CONFLICT(project_id,document_key,profile_id) DO UPDATE SET
                    source_id=excluded.source_id, original_path=excluded.original_path,
                    normalized_path=excluded.normalized_path, fingerprint=excluded.fingerprint,
                    status=excluded.status, error_code=excluded.error_code, updated_at=excluded.updated_at
                """,
                (identifier, project_id, document_key, source_id, document_kind, original_path,
                 normalized_path, fingerprint, PARSER_VERSION, self.profile.profile_id, status,
                 error_code, now, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM project_rag_documents WHERE project_id=? AND document_key=? AND profile_id=?",
                (project_id, document_key, self.profile.profile_id),
            ).fetchone()
        return dict(row)

    def _checked_embeddings(self, texts: list[str]) -> list[list[float]]:
        vectors = self.embedding.embed(texts)
        if len(vectors) != len(texts):
            raise EmbeddingError("embedding provider returned the wrong vector count")
        for vector in vectors:
            if len(vector) != self.profile.dimension:
                raise EmbeddingProfileMismatch("embedding vector dimension does not match profile")
        return vectors

    def index_internal(
        self, project_id: str, content_id: str, text: str, *, kind: str,
        task_id: Optional[str] = None, fingerprint: Optional[str] = None,
    ) -> dict[str, Any]:
        value = str(text or "")
        digest = fingerprint or fingerprint_text(value)
        chunks = chunk_text(value, chunk_chars=self.chunk_chars, overlap_chars=self.overlap_chars)
        return self._publish_generation(
            project_id, str(content_id), "internal", chunks, digest,
            source_id=None, task_id=task_id, kind=kind,
            original_path=None, normalized_path=None, keep_text=True,
        )

    def index_source(
        self, project_id: str, source: dict[str, Any], *, task_id: Optional[str] = None,
        kind: str = "source", expected_queue_fingerprint: Optional[str] = None,
    ) -> dict[str, Any]:
        source_id = str(source.get("id") or "").strip()
        path = str(source.get("normalized_path") or source.get("original_path") or "")
        if not source_id or not path:
            raise RAGError("source id and path are required")
        text, status = read_text_source(path, self.max_file_chars)
        if status not in {"ready", "empty"}:
            return self._set_document_status(
                project_id, source_id, "external", source_id=source_id,
                original_path=str(source.get("original_path") or path), normalized_path=path,
                fingerprint="", status=status, error_code=status,
            )
        digest = fingerprint_text(text)
        chunks = chunk_text(text, chunk_chars=self.chunk_chars, overlap_chars=self.overlap_chars)
        return self._publish_generation(
            project_id, source_id, "external", chunks, digest,
            source_id=source_id, task_id=task_id, kind=kind,
            original_path=str(source.get("original_path") or path),
            normalized_path=os.path.normcase(str(Path(path).resolve(strict=False))), keep_text=False,
            expected_queue_fingerprint=expected_queue_fingerprint,
        )

    def _publish_generation(
        self,
        project_id: str,
        document_key: str,
        document_kind: str,
        chunks: list[TextChunk],
        fingerprint: str,
        *,
        source_id: Optional[str],
        task_id: Optional[str],
        kind: str,
        original_path: Optional[str],
        normalized_path: Optional[str],
        keep_text: bool,
        expected_queue_fingerprint: Optional[str] = None,
    ) -> dict[str, Any]:
        document_id = self._document_identity(project_id, document_key, document_kind)
        texts = [chunk.text for chunk in chunks]
        vectors = self._checked_embeddings(texts) if texts else []
        now = time.time()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM project_rag_documents WHERE id=?", (document_id,)
            ).fetchone()
            if existing is not None and existing["status"] == "deleted":
                raise StaleIndexJob("document was deleted after this index job was queued")
            if (
                expected_queue_fingerprint is not None and existing is not None
                and existing["status"] == "queued"
                and existing["fingerprint"] != expected_queue_fingerprint
            ):
                raise StaleIndexJob("a newer source fingerprint has already been queued")
            if existing is not None and existing["fingerprint"] == fingerprint and existing["status"] == "published":
                return dict(existing)
            connection.execute(
                """
                INSERT INTO project_rag_documents
                    (id,project_id,document_key,source_id,document_kind,original_path,
                     normalized_path,fingerprint,parser_version,profile_id,status,error_code,
                     created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'building',NULL,?,?)
                ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,
                    original_path=excluded.original_path, normalized_path=excluded.normalized_path,
                    status='building', error_code=NULL, updated_at=excluded.updated_at
                """,
                (document_id, project_id, document_key, source_id, document_kind,
                 original_path, normalized_path, fingerprint, PARSER_VERSION,
                 self.profile.profile_id, now, now),
            )
            max_row = connection.execute(
                "SELECT COALESCE(MAX(generation_no),0) AS value FROM project_rag_generations WHERE document_id=?",
                (document_id,),
            ).fetchone()
            generation_no = int(max_row["value"]) + 1
            generation_id = _stable_id("raggen", document_id, generation_no, fingerprint)
            connection.execute(
                """
                INSERT INTO project_rag_generations
                    (id,document_id,generation_no,fingerprint,profile_id,status,chunk_count,created_at)
                VALUES (?,?,?,?,?,'building',?,?)
                """,
                (generation_id, document_id, generation_no, fingerprint,
                 self.profile.profile_id, len(chunks), now),
            )
            for chunk, vector in zip(chunks, vectors):
                terms = Counter(tokenize(chunk.text))
                chunk_id = _stable_id(
                    "ragchunk", document_id, fingerprint, self.profile.profile_id,
                    chunk.start_offset, chunk.end_offset,
                )
                connection.execute(
                    """
                    INSERT INTO project_rag_chunks
                        (id,generation_id,project_id,document_id,source_id,task_id,kind,
                         start_offset,end_offset,internal_text,term_freq_json,token_count,
                         embedding_json,fingerprint,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (chunk_id, generation_id, project_id, document_id, source_id, task_id, kind,
                     chunk.start_offset, chunk.end_offset, chunk.text if keep_text else None,
                     json.dumps(terms, ensure_ascii=False, separators=(",", ":")),
                     sum(terms.values()), json.dumps(vector, separators=(",", ":")),
                     fingerprint, now),
                )
            connection.execute(
                "UPDATE project_rag_generations SET status='superseded' "
                "WHERE document_id=? AND status='published'", (document_id,)
            )
            connection.execute(
                "UPDATE project_rag_generations SET status='published',published_at=? WHERE id=?",
                (now, generation_id),
            )
            connection.execute(
                """
                UPDATE project_rag_documents SET fingerprint=?,active_generation=?,status='published',
                    error_code=NULL,updated_at=? WHERE id=?
                """,
                (fingerprint, generation_id, now, document_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM project_rag_documents WHERE id=?", (document_id,)
            ).fetchone()
        return dict(row)

    def enqueue_source(self, project_id: str, source: dict[str, Any]) -> dict[str, Any]:
        source_id = str(source.get("id") or "")
        path = str(source.get("normalized_path") or source.get("original_path") or "")
        fingerprint = str(source.get("content_hash") or f"{source.get('size')}:{source.get('mtime_ns')}")
        document = self._set_document_status(
            project_id, source_id, "external", source_id=source_id,
            original_path=str(source.get("original_path") or path), normalized_path=path,
            fingerprint=fingerprint, status="queued", error_code=None,
        )
        key = _stable_id("ragjob", project_id, source_id, fingerprint, self.profile.profile_id, "ingest")
        now = time.time()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO project_rag_jobs
                    (idempotency_key,project_id,document_id,fingerprint,profile_id,operation,
                     status,attempt_count,created_at,updated_at)
                VALUES (?,?,?,?,?,'ingest','pending',0,?,?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status=CASE WHEN status='done' THEN status ELSE 'pending' END, updated_at=excluded.updated_at
                """,
                (key, project_id, document["id"], fingerprint, self.profile.profile_id, now, now),
            )
            connection.commit()
        return {"job_id": key, "status": "pending", "document_id": document["id"]}

    def pending_jobs(self, project_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        clauses = ["status IN ('pending','failed')"]
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        params.append(max(1, min(100, int(limit))))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM project_rag_jobs WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at LIMIT ?", params,
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_job(self, job_id: str, *, ok: bool, error: str = "") -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE project_rag_jobs
                SET status=CASE WHEN status='deleted' THEN status ELSE ? END,
                    attempt_count=attempt_count+1,
                    last_error=?,updated_at=? WHERE idempotency_key=?
                """,
                ("done" if ok else "failed", None if ok else str(error)[:1000], time.time(), job_id),
            )
            connection.commit()

    def supersede_job(self, job_id: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE project_rag_jobs SET status='superseded',updated_at=? "
                "WHERE idempotency_key=? AND status!='deleted'",
                (time.time(), job_id),
            )
            connection.commit()

    def _candidate_rows(
        self, project_id: str, *, task_id: Optional[str], kinds: Optional[Iterable[str]],
        source_ids: Optional[Iterable[str]],
    ) -> list[sqlite3.Row]:
        clauses = [
            "c.project_id=?", "d.profile_id=?", "d.status='published'",
            "g.status='published'", "d.active_generation=g.id",
        ]
        params: list[Any] = [project_id, self.profile.profile_id]
        if task_id is not None:
            clauses.append("c.task_id=?")
            params.append(task_id)
        selected_kinds = [str(value) for value in (kinds or []) if value]
        if selected_kinds:
            clauses.append(f"c.kind IN ({','.join('?' for _ in selected_kinds)})")
            params.extend(selected_kinds)
        selected_sources = [str(value) for value in (source_ids or []) if value]
        if selected_sources:
            clauses.append(f"c.source_id IN ({','.join('?' for _ in selected_sources)})")
            params.extend(selected_sources)
        params.append(self.candidate_limit)
        with closing(self.connect()) as connection:
            return connection.execute(
                """
                SELECT c.*,d.normalized_path,d.original_path,d.document_kind
                FROM project_rag_chunks c
                JOIN project_rag_generations g ON g.id=c.generation_id
                JOIN project_rag_documents d ON d.id=c.document_id
                WHERE """ + " AND ".join(clauses) + " ORDER BY c.id LIMIT ?",
                params,
            ).fetchall()

    def _bm25(self, query_tokens: list[str], rows: list[sqlite3.Row]) -> dict[str, float]:
        if not query_tokens or not rows:
            return {str(row["id"]): 0.0 for row in rows}
        terms = set(query_tokens)
        decoded = {str(row["id"]): json.loads(row["term_freq_json"] or "{}") for row in rows}
        df = {term: sum(1 for counts in decoded.values() if counts.get(term, 0)) for term in terms}
        avg_len = sum(max(1, int(row["token_count"])) for row in rows) / len(rows)
        n = len(rows)
        result: dict[str, float] = {}
        for row in rows:
            chunk_id = str(row["id"])
            counts = decoded[chunk_id]
            length = max(1, int(row["token_count"]))
            score = 0.0
            for term in query_tokens:
                frequency = float(counts.get(term, 0))
                if not frequency:
                    continue
                inverse = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
                denominator = frequency + 1.5 * (1.0 - 0.75 + 0.75 * length / avg_len)
                score += inverse * frequency * 2.5 / denominator
            result[chunk_id] = score
        return result

    def _cosine(self, left: list[float], right: list[float]) -> float:
        if len(left) != self.profile.dimension or len(right) != self.profile.dimension:
            raise EmbeddingProfileMismatch("query and indexed vector dimensions differ")
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)

    def search(
        self,
        project_id: str,
        query: str,
        *,
        task_id: Optional[str] = None,
        kinds: Optional[Iterable[str]] = None,
        source_ids: Optional[Iterable[str]] = None,
        top_k: int = 8,
        per_source_limit: int = 3,
        lexical_weight: float = 0.45,
        vector_weight: float = 0.55,
    ) -> list[RAGHit]:
        text = str(query or "").strip()
        if not text:
            return []
        limit = max(1, min(50, int(top_k)))
        per_source = max(1, min(limit, int(per_source_limit)))
        lexical_weight = max(0.0, float(lexical_weight))
        vector_weight = max(0.0, float(vector_weight))
        if lexical_weight + vector_weight <= 0:
            raise ValueError("at least one hybrid weight must be positive")
        rows = self._candidate_rows(
            project_id, task_id=task_id, kinds=kinds, source_ids=source_ids
        )
        if not rows:
            return []
        lexical = self._bm25(tokenize(text), rows)
        query_vectors = self._checked_embeddings([text])
        query_vector = query_vectors[0]
        vector = {
            str(row["id"]): self._cosine(query_vector, json.loads(row["embedding_json"]))
            for row in rows
        }
        max_lexical = max(lexical.values(), default=0.0)
        positive_vectors = {key: max(0.0, value) for key, value in vector.items()}
        max_vector = max(positive_vectors.values(), default=0.0)
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            chunk_id = str(row["id"])
            lexical_norm = lexical[chunk_id] / max_lexical if max_lexical else 0.0
            vector_norm = positive_vectors[chunk_id] / max_vector if max_vector else 0.0
            score = lexical_weight * lexical_norm + vector_weight * vector_norm
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1]["id"])))
        counts: Counter[str] = Counter()
        hits: list[RAGHit] = []
        for score, row in scored:
            source_key = str(row["source_id"] or row["document_id"])
            if counts[source_key] >= per_source:
                continue
            counts[source_key] += 1
            content, stale = self._materialize(row)
            hits.append(RAGHit(
                chunk_id=str(row["id"]), document_id=str(row["document_id"]),
                source_id=row["source_id"], task_id=row["task_id"], kind=str(row["kind"]),
                content=content, score=score, lexical_score=lexical[str(row["id"])],
                vector_score=vector[str(row["id"])], start_offset=int(row["start_offset"]),
                end_offset=int(row["end_offset"]), fingerprint=str(row["fingerprint"]),
                profile_id=self.profile.profile_id, path=row["normalized_path"], stale=stale,
            ))
            if len(hits) >= limit:
                break
        return hits

    def _materialize(self, row: sqlite3.Row) -> tuple[str, bool]:
        if row["document_kind"] == "internal":
            return str(row["internal_text"] or ""), False
        text, status = read_text_source(str(row["normalized_path"]), self.max_file_chars)
        if status not in {"ready", "empty"} or fingerprint_text(text) != row["fingerprint"]:
            with closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE project_rag_documents SET status='stale',error_code=?,updated_at=? WHERE id=?",
                    (status if status not in {"ready", "empty"} else "fingerprint_changed",
                     time.time(), row["document_id"]),
                )
                connection.commit()
            return "", True
        return text[int(row["start_offset"]):int(row["end_offset"])], False

    def remove_document(self, project_id: str, source_or_content_id: str) -> int:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id FROM project_rag_documents WHERE project_id=? AND (source_id=? OR document_key=?)",
                (project_id, source_or_content_id, source_or_content_id),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "DELETE FROM project_rag_generations WHERE document_id=?", (row["id"],)
                )
                connection.execute(
                    "UPDATE project_rag_documents SET active_generation=NULL,status='deleted',"
                    "error_code='deleted',updated_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
                connection.execute(
                    "UPDATE project_rag_jobs SET status='deleted',updated_at=? WHERE document_id=?",
                    (time.time(), row["id"]),
                )
            connection.commit()
        return len(rows)

    def delete_project(self, project_id: str) -> int:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id FROM project_rag_documents WHERE project_id=?", (project_id,)
            ).fetchall()
            connection.execute("DELETE FROM project_rag_documents WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM project_rag_jobs WHERE project_id=?", (project_id,))
            connection.commit()
        return len(rows)
