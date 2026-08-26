"""Provider-neutral Hybrid RAG primitives."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from pillow_assistant.core.project_memory_backend import BackendCapabilities


class RAGError(Exception):
    pass


class EmbeddingError(RAGError):
    pass


class EmbeddingProfileMismatch(EmbeddingError):
    pass


class ContentUploadDenied(RAGError):
    pass


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def tokenize(text: str, *, max_tokens: int = 200_000) -> list[str]:
    value = str(text or "")
    result = [match.group(0).lower() for match in _WORD_RE.finditer(value)]
    for match in _CJK_RUN_RE.finditer(value):
        run = match.group(0)
        if len(run) == 1:
            result.append(run)
        else:
            result.extend(run[index : index + 2] for index in range(len(run) - 1))
    return result[: max(1, int(max_tokens))]


@dataclass(frozen=True)
class EmbeddingProfile:
    provider_id: str
    model_id: str
    dimension: int
    metric: str = "cosine"

    @property
    def profile_id(self) -> str:
        payload = json.dumps({
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "dimension": self.dimension,
            "metric": self.metric,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@runtime_checkable
class EmbeddingProvider(Protocol):
    provider_id: str
    model_id: str
    dimension: int
    is_remote: bool

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def health(self) -> dict: ...


@runtime_checkable
class RAGProvider(Protocol):
    capabilities: BackendCapabilities

    def ensure_schema(self) -> None: ...
    def health(self) -> dict: ...
    def index_internal(self, project_id: str, content_id: str, text: str, **kwargs): ...
    def index_source(self, project_id: str, source: dict, **kwargs): ...
    def search(self, project_id: str, query: str, **kwargs) -> list["RAGHit"]: ...
    def delete_project(self, project_id: str) -> int: ...


class LocalHashEmbeddingProvider:
    """Small deterministic baseline; production deployments may replace it."""

    provider_id = "local"
    model_id = "feature-hash-v1"
    is_remote = False

    def __init__(self, dimension: int = 192) -> None:
        if int(dimension) < 16 or int(dimension) > 4096:
            raise ValueError("embedding dimension must be between 16 and 4096")
        self.dimension = int(dimension)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in tokenize(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
                index = int.from_bytes(digest[:8], "big") % self.dimension
                sign = 1.0 if digest[8] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors

    def health(self) -> dict:
        return {"ok": True, "provider_id": self.provider_id, "model_id": self.model_id,
                "dimension": self.dimension, "remote": False}


@dataclass(frozen=True)
class TextChunk:
    start_offset: int
    end_offset: int
    text: str


@dataclass
class RAGHit:
    chunk_id: str
    document_id: str
    source_id: Optional[str]
    task_id: Optional[str]
    kind: str
    content: str
    score: float
    lexical_score: float
    vector_score: float
    start_offset: int
    end_offset: int
    fingerprint: str
    profile_id: str
    path: Optional[str] = None
    stale: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UploadPolicy:
    provider_id: str
    project_id: str
    allow_content_upload: bool = False
    approved_at: Optional[float] = None
    policy_version: str = "1"


def guard_remote_upload(provider: EmbeddingProvider, project_id: str, policy: UploadPolicy) -> None:
    if not getattr(provider, "is_remote", False):
        return
    if policy.provider_id != provider.provider_id or policy.project_id != project_id:
        raise ContentUploadDenied("upload policy does not match provider and project")
    if not policy.allow_content_upload or policy.approved_at is None:
        raise ContentUploadDenied("remote content upload requires explicit project approval")


RAG_CAPABILITIES = BackendCapabilities(
    backend_id="local-hybrid-rag", keyword_search=True, vector_search=True,
    metadata_filter=True,
)
