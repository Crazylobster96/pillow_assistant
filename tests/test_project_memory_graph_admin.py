from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pillow_assistant.core.rag.graph_federation import FederatedGraphRAG


class GraphAdministrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "graph.db"
        self.graph = FederatedGraphRAG(self.db_path)
        self.graph.ensure_schema()
        self.project_id = "project-admin"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manual_assignment_overrides_routing_and_uses_revision_cas(self) -> None:
        inbox = self.graph.register_category(self.project_id, "Inbox", is_inbox=True)
        code = self.graph.register_category(
            self.project_id, "Code", routing_examples=["python source implementation"]
        )
        with self.assertRaises(ValueError):
            self.graph.assign_category(
                self.project_id, "memory_item", "m-1", code["id"], base_revision=1
            )

        assigned = self.graph.assign_category(
            self.project_id, "memory_item", "m-1", code["id"], reason="reviewed"
        )
        self.assertEqual(1, assigned["revision"])
        route = self.graph.resolve_category(
            self.project_id, "unrelated text", subject_type="memory_item", subject_id="m-1"
        )
        self.assertEqual(code["id"], route[0]["category"]["id"])
        self.assertEqual("manual-assignment", route[0]["reason"])

        with self.assertRaises(ValueError):
            self.graph.assign_category(
                self.project_id, "memory_item", "m-1", inbox["id"], base_revision=9
            )
        changed = self.graph.assign_category(
            self.project_id, "memory_item", "m-1", inbox["id"], base_revision=1
        )
        self.assertEqual(2, changed["revision"])
        self.assertEqual(inbox["id"], self.graph.get_assignment(
            self.project_id, "memory_item", "m-1"
        )["category_id"])

    def test_migration_copies_graph_relations_and_retargets_assignments(self) -> None:
        source = self.graph.register_category(self.project_id, "Legacy", is_inbox=True)
        target = self.graph.register_category(self.project_id, "Current")
        outside = self.graph.register_category(self.project_id, "Other")
        first = self.graph.upsert_node(
            self.project_id, source["id"], "one", node_type="entity",
            label="One", content="legacy one",
        )
        second = self.graph.upsert_node(
            self.project_id, source["id"], "two", node_type="entity",
            label="Two", content="legacy two",
        )
        external = self.graph.upsert_node(
            self.project_id, outside["id"], "outside", node_type="entity",
            label="Outside", content="outside",
        )
        self.graph.add_edge(
            self.project_id, source["id"], first["id"], second["id"], "depends_on",
            confidence=0.9, evidence={"check": "unit"},
        )
        self.graph.add_cross_link(
            self.project_id, first["id"], external["id"], "related_to",
            evidence={"source": "manual"},
        )
        self.graph.assign_category(
            self.project_id, "memory_item", "migrate-me", source["id"]
        )

        result = self.graph.migrate_category(source["id"], target["id"])

        self.assertEqual("done", result["status"])
        self.assertEqual(2, len(result["node_mapping"]))
        self.assertIsNone(self.graph._get_category(source["id"]))
        self.assertTrue(self.graph._get_category(target["id"])["is_inbox"])
        assignment = self.graph.get_assignment(
            self.project_id, "memory_item", "migrate-me"
        )
        self.assertEqual(target["id"], assignment["category_id"])
        self.assertEqual(2, assignment["revision"])

        with closing(self.graph.connect()) as connection:
            nodes = connection.execute(
                "SELECT * FROM project_graph_nodes WHERE category_id=? ORDER BY node_key",
                (target["id"],),
            ).fetchall()
            edges = connection.execute(
                "SELECT * FROM project_graph_edges WHERE category_id=?",
                (target["id"],),
            ).fetchall()
            links = connection.execute(
                "SELECT * FROM project_graph_cross_links WHERE project_id=?",
                (self.project_id,),
            ).fetchall()
            job = connection.execute(
                "SELECT * FROM project_graph_jobs WHERE id=?", (result["job_id"],)
            ).fetchone()
        self.assertEqual(["one", "two"], [row["node_key"] for row in nodes])
        self.assertEqual(1, len(edges))
        self.assertEqual("depends_on", edges[0]["relation_type"])
        self.assertEqual(1, len(links))
        self.assertIn(target["id"], {links[0]["from_category_id"], links[0]["to_category_id"]})
        self.assertEqual("done", job["status"])
        self.assertEqual(result["node_mapping"], json.loads(job["payload_json"])["node_mapping"])

    def test_migration_does_not_truncate_more_than_one_hundred_cross_links(self) -> None:
        source = self.graph.register_category(self.project_id, "Many links")
        target = self.graph.register_category(self.project_id, "Many links target")
        outside = self.graph.register_category(self.project_id, "Many links outside")
        root = self.graph.upsert_node(
            self.project_id, source["id"], "root", node_type="entity",
            label="Root", content="root",
        )
        node_rows = []
        link_rows = []
        for index in range(105):
            node_id = f"outside-{index:03d}"
            node_rows.append((
                node_id, self.project_id, outside["id"], node_id, "entity", node_id,
                "outside", f"fingerprint-{index}", 1.0, 1.0,
            ))
            link_rows.append((
                f"cross-{index:03d}", self.project_id, source["id"], root["id"],
                outside["id"], node_id, "related_to", 1.0, 1.0, 1.0,
            ))
        with closing(self.graph.connect()) as connection:
            connection.executemany(
                """
                INSERT INTO project_graph_nodes
                    (id,project_id,category_id,node_key,node_type,label,content,fingerprint,
                     revision,validity,provenance_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?, ?,1,'active','{}',?,?)
                """,
                node_rows,
            )
            connection.executemany(
                """
                INSERT INTO project_graph_cross_links
                    (id,project_id,from_category_id,from_node_id,to_category_id,to_node_id,
                     relation_type,weight,evidence_json,validity,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'{}','active',?,?)
                """,
                link_rows,
            )
            connection.commit()
        self.graph.migrate_category(source["id"], target["id"])
        with closing(self.graph.connect()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM project_graph_cross_links WHERE project_id=?",
                (self.project_id,),
            ).fetchone()[0]
        self.assertEqual(105, count)

    def test_parent_with_children_must_be_migrated_explicitly(self) -> None:
        source = self.graph.register_category(self.project_id, "Parent")
        self.graph.register_category(self.project_id, "Child", parent_id=source["id"])
        target = self.graph.register_category(self.project_id, "Target")
        with self.assertRaises(ValueError):
            self.graph.migrate_category(source["id"], target["id"])


if __name__ == "__main__":
    unittest.main()
