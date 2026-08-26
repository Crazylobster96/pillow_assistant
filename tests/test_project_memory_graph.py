from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pillow_assistant.core.project_memory_backend import BackendCapabilityError
from pillow_assistant.core.rag.base import ContentUploadDenied, UploadPolicy
from pillow_assistant.core.rag.graph_federation import (
    CategoryCycleError,
    CrossCategoryEdgeError,
    FederatedGraphRAG,
    GraphFederationError,
)
from pillow_assistant.core.rag.level3_backend import Level3ProjectMemoryBackend
from pillow_assistant.core.rag.multimodal import (
    ExtractionResult,
    MetadataOnlyExtractor,
    MultimodalAssetStore,
)
from storage.project_memory import ProjectMemoryStore
from storage.projects import ProjectStore


class RemoteImageExtractor:
    provider_id = "remote-image"
    profile_id = "remote-image-v1"
    modalities = {"image"}
    is_remote = True

    def extract(self, path, modality, locator=None):
        return ExtractionResult(
            status="extracted", description="remote description",
            locator=locator or {"bbox": [0, 0, 10, 10]},
        )

    def health(self):
        return {"ok": True}


class Level3GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = ProjectStore(self.root / "projects")
        self.project = self.projects.create("graph")
        self.structured = ProjectMemoryStore(self.root / "assistant.db", self.projects.base)
        self.structured.ensure_schema()
        self.structured.ensure_project(self.project.id)
        self.graph = FederatedGraphRAG(self.root / "assistant.db")
        self.graph.ensure_schema()
        self.assets = MultimodalAssetStore(self.root / "assistant.db", self.graph)
        self.assets.ensure_schema()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_category_tree_rejects_cross_project_parent_and_cycle(self) -> None:
        root = self.graph.register_category(
            self.project.id, "Software", routing_examples=["python software code"]
        )
        child = self.graph.register_category(
            self.project.id, "Python", parent_id=root["id"],
            routing_examples=["python function class"],
        )
        with self.assertRaises(CategoryCycleError):
            self.graph.update_category(root["id"], base_revision=1, parent_id=child["id"])
        other = self.projects.create("other")
        foreign = self.graph.register_category(other.id, "Foreign")
        with self.assertRaises(GraphFederationError):
            self.graph.register_category(self.project.id, "Invalid", parent_id=foreign["id"])

    def test_nonempty_category_requires_explicit_cascade_and_inbox_replacement(self) -> None:
        inbox = self.graph.register_category(self.project.id, "Inbox", is_inbox=True)
        replacement = self.graph.register_category(self.project.id, "Replacement")
        child = self.graph.register_category(
            self.project.id, "Child", parent_id=inbox["id"]
        )
        self.graph.upsert_node(
            self.project.id, child["id"], "item", node_type="fact",
            label="Item", content="content",
        )
        with self.assertRaises(GraphFederationError):
            self.graph.delete_category(inbox["id"])
        with self.assertRaises(GraphFederationError):
            self.graph.delete_category(inbox["id"], cascade=True)
        deleted = self.graph.delete_category(
            inbox["id"], cascade=True, replacement_inbox_id=replacement["id"]
        )
        self.assertEqual(2, deleted["categories"])
        self.assertEqual(1, deleted["nodes"])
        refreshed = self.graph._get_category(replacement["id"])
        self.assertTrue(refreshed["is_inbox"])
        self.assertIsNone(self.graph._get_category(inbox["id"]))
        self.assertIsNone(self.graph._get_category(child["id"]))

    def test_one_inbox_and_below_threshold_routing(self) -> None:
        inbox = self.graph.register_category(self.project.id, "Inbox", is_inbox=True)
        with self.assertRaises(GraphFederationError):
            self.graph.register_category(self.project.id, "Another inbox", is_inbox=True)
        self.graph.register_category(self.project.id, "Code", routing_examples=["python code"])
        routes = self.graph.classify(self.project.id, "unrelated", threshold=1.1)
        self.assertEqual(inbox["id"], routes[0]["category"]["id"])
        self.assertEqual("below-threshold-inbox", routes[0]["reason"])

    def test_hierarchical_route_selects_child(self) -> None:
        root = self.graph.register_category(
            self.project.id, "Software", routing_examples=["python software code"]
        )
        child = self.graph.register_category(
            self.project.id, "Python", parent_id=root["id"],
            routing_examples=["python function class"],
        )
        routes = self.graph.classify(self.project.id, "python function", threshold=0.01)
        self.assertEqual(child["id"], routes[0]["category"]["id"])
        self.assertTrue(routes[0]["reason"].startswith("hierarchical:"))

    def test_category_update_revisions_and_reindexes_route(self) -> None:
        category = self.graph.register_category(self.project.id, "Docs", description="old docs")
        updated = self.graph.update_category(
            category["id"], base_revision=1, description="architecture diagrams",
            routing_examples=["system architecture"],
        )
        self.assertEqual(2, updated["revision"])
        routes = self.graph.classify(self.project.id, "system architecture", threshold=0.01)
        self.assertEqual(category["id"], routes[0]["category"]["id"])

    def test_node_upsert_is_idempotent_then_revisions(self) -> None:
        category = self.graph.register_category(self.project.id, "Code", routing_examples=["code"])
        first = self.graph.upsert_node(
            self.project.id, category["id"], "module-a", node_type="module",
            label="Module A", content="first",
        )
        same = self.graph.upsert_node(
            self.project.id, category["id"], "module-a", node_type="module",
            label="Module A", content="first",
        )
        changed = self.graph.upsert_node(
            self.project.id, category["id"], "module-a", node_type="module",
            label="Module A", content="second",
        )
        self.assertEqual(first["id"], same["id"])
        self.assertEqual(1, same["revision"])
        self.assertEqual(2, changed["revision"])

    def test_edges_are_category_isolated_and_cross_links_are_global(self) -> None:
        left_cat = self.graph.register_category(self.project.id, "Left", routing_examples=["left"])
        right_cat = self.graph.register_category(self.project.id, "Right", routing_examples=["right"])
        left_a = self.graph.upsert_node(
            self.project.id, left_cat["id"], "a", node_type="entity", label="A"
        )
        left_b = self.graph.upsert_node(
            self.project.id, left_cat["id"], "b", node_type="entity", label="B"
        )
        right = self.graph.upsert_node(
            self.project.id, right_cat["id"], "r", node_type="entity", label="R"
        )
        edge = self.graph.add_edge(
            self.project.id, left_cat["id"], left_a["id"], left_b["id"], "depends_on"
        )
        self.assertEqual(left_cat["id"], edge["category_id"])
        with self.assertRaises(CrossCategoryEdgeError):
            self.graph.add_edge(
                self.project.id, left_cat["id"], left_a["id"], right["id"], "invalid"
            )
        link = self.graph.add_cross_link(
            self.project.id, left_a["id"], right["id"], "references"
        )
        self.assertNotEqual(link["from_category_id"], link["to_category_id"])
        with closing(self.graph.connect()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM project_graph_edges"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM project_graph_cross_links"
            ).fetchone()[0])

    def test_category_vector_namespaces_do_not_cross(self) -> None:
        code = self.graph.register_category(self.project.id, "Code", routing_examples=["python"])
        legal = self.graph.register_category(self.project.id, "Legal", routing_examples=["contract"])
        self.graph.upsert_node(
            self.project.id, code["id"], "python", node_type="module",
            label="Python module", content="python function",
        )
        self.graph.upsert_node(
            self.project.id, legal["id"], "contract", node_type="document",
            label="Contract", content="legal contract",
        )
        code_hits = self.graph.search_category(self.project.id, code["id"], "python")
        legal_hits = self.graph.search_category(self.project.id, legal["id"], "python")
        self.assertTrue(code_hits)
        self.assertFalse(any(hit["node"]["label"] == "Python module" for hit in legal_hits))

    def test_bounded_traversal_returns_traceable_paths(self) -> None:
        category = self.graph.register_category(self.project.id, "Graph", routing_examples=["graph"])
        nodes = [self.graph.upsert_node(
            self.project.id, category["id"], str(index), node_type="entity",
            label=f"Node {index}", content="graph node",
        ) for index in range(4)]
        edges = [self.graph.add_edge(
            self.project.id, category["id"], nodes[index]["id"], nodes[index + 1]["id"], "next"
        ) for index in range(3)]
        shallow = self.graph.traverse(category["id"], [nodes[0]["id"]], depth=1)
        deep = self.graph.traverse(category["id"], [nodes[0]["id"]], depth=3, max_nodes=3)
        self.assertEqual(2, len(shallow["nodes"]))
        self.assertEqual([edges[0]["id"]], shallow["paths"][nodes[1]["id"]])
        self.assertEqual(3, len(deep["nodes"]))
        self.assertTrue(deep["truncated"])

    def test_federated_plan_and_merge_obey_category_and_total_limits(self) -> None:
        for name in ("Alpha", "Beta", "Gamma"):
            category = self.graph.register_category(
                self.project.id, name, routing_examples=["shared topic"]
            )
            self.graph.upsert_node(
                self.project.id, category["id"], name, node_type="fact",
                label=name, content="shared topic fact",
            )
        result = self.graph.search(
            self.project.id, "shared topic", top_categories=2,
            per_category_top_k=3, total_limit=2, per_category_limit=1,
        )
        self.assertLessEqual(len(result["plan"]["routes"]), 2)
        self.assertLessEqual(len(result["hits"]), 2)
        self.assertEqual(len(result["hits"]), len({hit["category_id"] for hit in result["hits"]}))

    def test_one_category_failure_returns_partial_results(self) -> None:
        categories = []
        for name in ("One", "Two"):
            category = self.graph.register_category(
                self.project.id, name, routing_examples=["shared"]
            )
            categories.append(category)
            self.graph.upsert_node(
                self.project.id, category["id"], name, node_type="fact",
                label=name, content="shared",
            )
        failed_id = categories[0]["id"]
        original = self.graph.search_category

        def sometimes_fails(project_id, category_id, query, **kwargs):
            if category_id == failed_id:
                raise RuntimeError("provider offline")
            return original(project_id, category_id, query, **kwargs)

        self.graph.search_category = sometimes_fails
        result = self.graph.search(self.project.id, "shared", top_categories=2)
        self.assertTrue(result["partial"])
        self.assertEqual(failed_id, result["failures"][0]["category_id"])
        self.assertTrue(result["hits"])

    def test_metadata_only_asset_records_path_without_copying_file(self) -> None:
        category = self.graph.register_category(
            self.project.id, "Images", routing_examples=["image"], modalities=["image"]
        )
        image = self.root / "diagram.png"
        image.write_bytes(b"not-a-real-image-but-external")
        asset = self.assets.register_asset(
            self.project.id, category["id"], image, extractor=MetadataOnlyExtractor()
        )
        self.assertEqual("metadata_only", asset["status"])
        self.assertEqual(str(image.resolve()).lower(), asset["normalized_path"].lower())
        self.assertFalse((self.project.root / image.name).exists())
        self.assertIn("diagram.png", asset["description"])

    def test_asset_change_stales_asset_and_graph_node(self) -> None:
        category = self.graph.register_category(self.project.id, "Assets", modalities=["image"])
        image = self.root / "asset.png"
        image.write_bytes(b"v1")
        asset = self.assets.register_asset(self.project.id, category["id"], image)
        image.write_bytes(b"version-two")
        changed = self.assets.refresh_asset(asset["id"])
        self.assertEqual("stale", changed["status"])
        self.assertEqual("stale", self.graph.get_node(asset["node_id"])["validity"])

    def test_remote_multimodal_extractor_requires_upload_approval(self) -> None:
        category = self.graph.register_category(self.project.id, "Remote", modalities=["image"])
    def test_delete_asset_removes_derived_node_edges_and_vector(self) -> None:
        category = self.graph.register_category(self.project.id, "Asset delete", modalities=["image"])
        image = self.root / "deletable_unique.png"
        image.write_bytes(b"asset")
        asset = self.assets.register_asset(self.project.id, category["id"], image)
        peer = self.graph.upsert_node(
            self.project.id, category["id"], "peer", node_type="entity",
            label="Peer", content="unrelated peer",
        )
        self.graph.add_edge(
            self.project.id, category["id"], asset["node_id"], peer["id"], "references"
        )

        self.assertTrue(self.assets.delete_asset(asset["id"]))
        self.assertFalse(self.assets.delete_asset(asset["id"]))
        self.assertIsNone(self.assets.get_asset(asset["id"]))
        self.assertIsNone(self.graph.get_node(asset["node_id"]))
        with closing(self.graph.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM project_graph_edges WHERE from_node_id=? OR to_node_id=?",
                (asset["node_id"], asset["node_id"]),
            ).fetchone()[0])
            document = connection.execute(
                "SELECT status FROM project_rag_documents WHERE project_id=? AND document_key=?",
                (self.graph._category_namespace(self.project.id, category["id"]), asset["node_id"]),
            ).fetchone()
        self.assertEqual("deleted", document["status"])

        image = self.root / "remote.png"
        image.write_bytes(b"image")
        extractor = RemoteImageExtractor()
        with self.assertRaises(ValueError):
            self.assets.register_asset(
                self.project.id, category["id"], image, extractor=extractor
            )
        denied = UploadPolicy(provider_id=extractor.provider_id, project_id=self.project.id)
        with self.assertRaises(ContentUploadDenied):
            self.assets.register_asset(
                self.project.id, category["id"], image,
                extractor=extractor, upload_policy=denied,
            )
        approved = UploadPolicy(
            provider_id=extractor.provider_id, project_id=self.project.id,
            allow_content_upload=True, approved_at=1.0,
        )
        asset = self.assets.register_asset(
            self.project.id, category["id"], image,
            extractor=extractor, upload_policy=approved,
        )
        self.assertEqual("extracted", asset["status"])
        self.assertEqual({"bbox": [0, 0, 10, 10]}, asset["locator"])

    def test_level3_backend_indexes_and_retrieves_memory(self) -> None:
        self.graph.register_category(
            self.project.id, "Decisions", routing_examples=["architecture decision sqlite"]
        )
        backend = Level3ProjectMemoryBackend(self.structured, self.graph)
        item = backend.add_memory_item(
            self.project.id, "decision", "architecture decision uses sqlite"
        )
        self.assertEqual("published", item["graph_index_status"])
        hits = backend.search_memory(self.project.id, "sqlite architecture")
        self.assertTrue(hits)
        self.assertEqual("graph_rag", hits[0]["source_type"])

    def test_level3_replace_rejects_incomplete_control_backend(self) -> None:
        with self.assertRaises(BackendCapabilityError):
            Level3ProjectMemoryBackend(object(), self.graph, mode="replace")

    def test_delete_project_clears_graph_vectors_and_assets(self) -> None:
        category = self.graph.register_category(self.project.id, "Delete", routing_examples=["delete"])
        self.graph.upsert_node(
            self.project.id, category["id"], "node", node_type="fact",
            label="Delete node", content="delete",
        )
        image = self.root / "delete.png"
        image.write_bytes(b"asset")
        self.assets.register_asset(self.project.id, category["id"], image)
        self.assertEqual(1, self.assets.delete_project(self.project.id))
        deleted = self.graph.delete_project(self.project.id)
        self.assertEqual(1, deleted["categories"])
        self.assertEqual([], self.graph.list_categories(self.project.id))
        with closing(self.graph.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM project_graph_nodes WHERE project_id=?", (self.project.id,)
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
