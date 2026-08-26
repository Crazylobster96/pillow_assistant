"""Protocol for category-scoped local or SaaS GraphRAG providers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pillow_assistant.core.project_memory_backend import BackendCapabilities


@runtime_checkable
class CategoryGraphProvider(Protocol):
    provider_id: str
    graph_capabilities: dict[str, bool]
    capabilities: BackendCapabilities
    is_remote: bool

    def health(self) -> dict[str, Any]: ...
    def upsert_node(self, project_id: str, category_id: str, node: dict[str, Any]) -> None: ...
    def upsert_edge(self, project_id: str, category_id: str, edge: dict[str, Any]) -> None: ...
    def search_category(
        self, project_id: str, category_id: str, query: str, **kwargs: Any
    ) -> list[dict[str, Any]]: ...
    def delete_category(self, project_id: str, category_id: str) -> None: ...


def validate_graph_provider(provider: Any, provider_id: str) -> None:
    if not isinstance(provider, CategoryGraphProvider):
        raise TypeError(f"GraphRAG provider '{provider_id}' does not implement the category protocol")
    features = provider.graph_capabilities
    required_features = {"community_summaries", "graph_traversal", "node_delete"}
    if not isinstance(features, dict) or not required_features.issubset(features):
        raise ValueError(
            f"GraphRAG provider '{provider_id}' must declare: "
            + ", ".join(sorted(required_features))
        )
    if any(not isinstance(features[name], bool) for name in required_features):
        raise ValueError("GraphRAG provider feature declarations must be boolean")
    if str(provider.provider_id) != str(provider_id):
        raise ValueError("GraphRAG provider registry key does not match provider_id")
    capabilities = provider.capabilities.enabled()
    missing = {"keyword_search", "vector_search", "metadata_filter"} - capabilities
    if missing:
        raise ValueError(
            f"GraphRAG provider '{provider_id}' is missing: " + ", ".join(sorted(missing))
        )
