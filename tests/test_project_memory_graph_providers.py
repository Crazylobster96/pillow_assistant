from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pillow_assistant.core.project_memory_backend import BackendCapabilities
from pillow_assistant.core.rag.graph_federation import (
    FederatedGraphRAG,
    GraphFederationError,
)


class FakeCategoryProvider:
    provider_id = "fake-saas"
    is_remote = True
    capabilities = BackendCapabilities(
        backend_id=provider_id, keyword_search=True, vector_search=True, metadata_filter=True
    )
    graph_capabilities = {"community_summaries": False, "graph_traversal": True,
                          "node_delete": False}

    def __init__(self) -> None:
        self.nodes = []
        self.edges = []
        self.deleted = []
        self.return_wrong_category = False

    def health(self):
        return {"ok": True, "provider_id": self.provider_id}

    def upsert_node(self, project_id, category_id, node):
        self.nodes.append((project_id, category_id, node))

    def upsert_edge(self, project_id, category_id, edge):
        self.edges.append((project_id, category_id, edge))

    def search_category(self, project_id, category_id, query, **kwargs):
        node = self.nodes[-1][2]
        return [{
            "category_id": "wrong" if self.return_wrong_category else category_id,
            "node": node, "score": 0.9, "seed_node_id": node["id"],
            "path_edge_ids": [], "profile_id": self.provider_id,
        }]

    def delete_category(self, project_id, category_id):
        self.deleted.append((project_id, category_id))


class GraphProviderRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "graph.db"
        self.provider = FakeCategoryProvider()
        self.graph = FederatedGraphRAG(
            self.db_path, providers={self.provider.provider_id: self.provider}
        )
        self.graph.ensure_schema()
        self.project_id = "project-provider"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unknown_provider_binding_is_rejected(self) -> None:
        with self.assertRaises(GraphFederationError):
            self.graph.register_category(
                self.project_id, "Missing", backend_id="not-installed"
            )

    def test_node_edge_search_and_delete_route_to_bound_provider(self) -> None:
        category = self.graph.register_category(
            self.project_id, "SaaS", backend_id=self.provider.provider_id,
            routing_examples=["hosted graph"],
        )
        first = self.graph.upsert_node(
            self.project_id, category["id"], "one", node_type="entity",
            label="One", content="hosted graph",
        )
        second = self.graph.upsert_node(
            self.project_id, category["id"], "two", node_type="entity",
            label="Two", content="hosted graph",
        )
        self.graph.add_edge(
            self.project_id, category["id"], first["id"], second["id"], "related"
        )
        hits = self.graph.search_category(
            self.project_id, category["id"], "hosted graph"
        )
        self.assertEqual(2, len(self.provider.nodes))
        self.assertEqual(1, len(self.provider.edges))
        self.assertEqual(category["id"], hits[0]["category_id"])
        self.graph.delete_category(category["id"], cascade=True)
        self.assertEqual([(self.project_id, category["id"])], self.provider.deleted)

    def test_provider_cannot_return_cross_category_hits(self) -> None:
        category = self.graph.register_category(
            self.project_id, "SaaS", backend_id=self.provider.provider_id
        )
        self.graph.upsert_node(
            self.project_id, category["id"], "one", node_type="entity", label="One"
        )
        self.provider.return_wrong_category = True
        with self.assertRaises(GraphFederationError):
            self.graph.search_category(self.project_id, category["id"], "one")


if __name__ == "__main__":
    unittest.main()
