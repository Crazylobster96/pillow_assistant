from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pillow_assistant.core.rag.level3_backend import (
    Level3ProjectMemoryBackend,
    build_level3_backend,
)
from pillow_assistant.core.rag.project_backend import (
    Layer2ProjectMemoryBackend,
    build_layer2_backend,
)
from storage.project_memory import ProjectMemoryStore
from storage.projects import ProjectStore


class ProjectMemoryBackendFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = ProjectStore(self.root / "projects")
        self.project = self.projects.create("runtime")
        self.store = ProjectMemoryStore(self.root / "assistant.db", self.projects.base)
        self.store.ensure_schema()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_disabled_layers_return_the_lower_backend_unchanged(self) -> None:
        self.assertIs(self.store, build_layer2_backend(self.store, self.root / "assistant.db", {}))
        self.assertIs(self.store, build_level3_backend(self.store, self.root / "assistant.db", {}))

    def test_layer2_local_augment_factory_and_unknown_provider(self) -> None:
        backend = build_layer2_backend(self.store, self.root / "assistant.db", {
            "mode": "augment", "provider": "local", "chunk_chars": 512,
        })
        self.assertIsInstance(backend, Layer2ProjectMemoryBackend)
        self.assertTrue(backend.rag.health()["ok"])
        with self.assertRaises(ValueError):
            build_layer2_backend(self.store, self.root / "assistant.db", {
                "mode": "augment", "provider": "not-installed",
            })

    def test_layer3_templates_are_instantiated_per_project_and_idempotent(self) -> None:
        backend = build_level3_backend(self.store, self.root / "assistant.db", {
            "mode": "augment", "provider": "local",
            "categories": [
                {"name": "Inbox", "is_inbox": True},
                {"name": "Software", "routing_examples": ["python software"]},
                {"name": "Python", "parent": "Software", "routing_examples": ["python function"]},
            ],
        })
        self.assertIsInstance(backend, Level3ProjectMemoryBackend)
        backend.ensure_project(self.project.id)
        backend.ensure_project(self.project.id)
        categories = backend.federation.list_categories(self.project.id)
        self.assertEqual(3, len(categories))
        python = next(category for category in categories if category["name"] == "Python")
        software = next(category for category in categories if category["name"] == "Software")
        self.assertEqual(software["id"], python["parent_id"])

    def test_layer3_unknown_provider_is_not_silently_downgraded(self) -> None:
        with self.assertRaises(ValueError):
            build_level3_backend(self.store, self.root / "assistant.db", {
                "mode": "augment", "provider": "not-installed",
            })


if __name__ == "__main__":
    unittest.main()
