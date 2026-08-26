"""Optional project-memory retrieval backends."""

from .base import (
    ContentUploadDenied,
    EmbeddingError,
    EmbeddingProfile,
    EmbeddingProfileMismatch,
    LocalHashEmbeddingProvider,
    RAGError,
    RAGHit,
    UploadPolicy,
    guard_remote_upload,
)
from .graph_federation import FederatedGraphRAG
from .graph_provider import CategoryGraphProvider
from .level3_backend import Level3ProjectMemoryBackend
from .local_hybrid import LocalHybridRAG, StaleIndexJob
from .multimodal import MetadataOnlyExtractor, MultimodalAssetStore
from .project_backend import Layer2ProjectMemoryBackend

__all__ = [
    "CategoryGraphProvider", "ContentUploadDenied", "EmbeddingError", "EmbeddingProfile",
    "EmbeddingProfileMismatch", "FederatedGraphRAG",
    "Layer2ProjectMemoryBackend", "Level3ProjectMemoryBackend",
    "LocalHashEmbeddingProvider", "LocalHybridRAG", "RAGError", "RAGHit",
    "MetadataOnlyExtractor", "MultimodalAssetStore", "StaleIndexJob", "UploadPolicy",
    "guard_remote_upload",
]
