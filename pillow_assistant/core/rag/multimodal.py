"""Path-only multimodal asset references and extractor protocol."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from pillow_assistant.core.rag.base import UploadPolicy, guard_remote_upload


@dataclass
class ExtractionResult:
    status: str
    description: str = ""
    locator: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    entities: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class MultimodalExtractor(Protocol):
    provider_id: str
    profile_id: str
    modalities: set[str]
    is_remote: bool

    def extract(self, path: str, modality: str, locator: Optional[dict] = None) -> ExtractionResult: ...
    def health(self) -> dict: ...


class MetadataOnlyExtractor:
    provider_id = "local"
    profile_id = "metadata-only-v1"
    modalities = {"image", "audio", "video", "pdf", "document", "unknown"}
    is_remote = False

    def extract(self, path: str, modality: str, locator: Optional[dict] = None) -> ExtractionResult:
        candidate = Path(path)
        stat = candidate.stat()
        return ExtractionResult(
            status="metadata_only",
            description=f"{candidate.name} ({modality}, {stat.st_size} bytes)",
            locator=dict(locator or {}),
            metadata={"filename": candidate.name, "extension": candidate.suffix.lower(),
                      "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)},
        )

    def health(self) -> dict:
        return {"ok": True, "profile_id": self.profile_id, "metadata_only": True}


def infer_modality(path: str | Path) -> str:
    extension = Path(path).suffix.lower()
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}:
        return "image"
    if extension in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
        return "audio"
    if extension in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        return "video"
    if extension == ".pdf":
        return "pdf"
    if extension in {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}:
        return "document"
    return "unknown"


class MultimodalAssetStore:
    def __init__(self, db_path: str | Path, federation: Any) -> None:
        self.db_path = Path(db_path)
        self.federation = federation

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def ensure_schema(self) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS project_multimodal_assets (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, category_id TEXT NOT NULL,
                    source_id TEXT, node_id TEXT, original_path TEXT NOT NULL,
                    normalized_path TEXT NOT NULL, modality TEXT NOT NULL, size INTEGER,
                    mtime_ns INTEGER, fingerprint TEXT NOT NULL, extractor_profile TEXT NOT NULL,
                    status TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    locator_json TEXT NOT NULL DEFAULT '{}', metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(project_id,normalized_path,extractor_profile)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_mm_project_category ON project_multimodal_assets(project_id,category_id,status)"
            )
            connection.commit()

    @staticmethod
    def _fingerprint(size: int, mtime_ns: int) -> str:
        return hashlib.sha256(f"{size}:{mtime_ns}".encode("utf-8")).hexdigest()

    def register_asset(
        self,
        project_id: str,
        category_id: str,
        path: str | Path,
        *,
        source_id: Optional[str] = None,
        modality: Optional[str] = None,
        extractor: Optional[MultimodalExtractor] = None,
        upload_policy: Optional[UploadPolicy] = None,
        locator: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        category = self.federation._get_category(category_id)
        if category is None or category["project_id"] != project_id:
            raise ValueError("asset category must belong to project")
        candidate = Path(path).expanduser()
        normalized = os.path.normcase(str(candidate.resolve(strict=False)))
        stat = candidate.stat()
        selected_modality = modality or infer_modality(candidate)
        selected = extractor or MetadataOnlyExtractor()
        if selected_modality not in selected.modalities:
            raise ValueError("extractor does not support this modality")
        if selected.is_remote:
            if upload_policy is None:
                raise ValueError("remote multimodal extraction requires upload policy")
            guard_remote_upload(selected, project_id, upload_policy)
        extraction = selected.extract(normalized, selected_modality, locator)
        fingerprint = self._fingerprint(int(stat.st_size), int(stat.st_mtime_ns))
        identifier = f"asset_{uuid.uuid4().hex}"
        now = time.time()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                """
                SELECT id FROM project_multimodal_assets
                WHERE project_id=? AND normalized_path=? AND extractor_profile=?
                """,
                (project_id, normalized, selected.profile_id),
            ).fetchone()
            if existing is not None:
                identifier = str(existing["id"])
            connection.execute(
                """
                INSERT INTO project_multimodal_assets
                    (id,project_id,category_id,source_id,original_path,normalized_path,modality,
                     size,mtime_ns,fingerprint,extractor_profile,status,description,
                     locator_json,metadata_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,normalized_path,extractor_profile) DO UPDATE SET
                    category_id=excluded.category_id,source_id=excluded.source_id,
                    modality=excluded.modality,size=excluded.size,mtime_ns=excluded.mtime_ns,
                    fingerprint=excluded.fingerprint,status=excluded.status,
                    description=excluded.description,locator_json=excluded.locator_json,
                    metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
                """,
                (identifier, project_id, category_id, source_id, str(path), normalized,
                 selected_modality, int(stat.st_size), int(stat.st_mtime_ns), fingerprint,
                 selected.profile_id, extraction.status, extraction.description,
                 json_dumps(extraction.locator), json_dumps(extraction.metadata), now, now),
            )
            connection.commit()
        node = self.federation.upsert_node(
            project_id, category_id, f"asset:{identifier}", node_type=f"asset_{selected_modality}",
            label=candidate.name, content=extraction.description, source_id=source_id,
            fingerprint=fingerprint,
            provenance={"asset_id": identifier, "extractor_profile": selected.profile_id,
                        "status": extraction.status, "locator": extraction.locator},
        )
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE project_multimodal_assets SET node_id=? WHERE id=?", (node["id"], identifier)
            )
            connection.commit()
        return self.get_asset(identifier)

    def get_asset(self, asset_id: str) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_multimodal_assets WHERE id=?", (asset_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["locator"] = json_loads(item.pop("locator_json"))
        item["metadata"] = json_loads(item.pop("metadata_json"))
        return item

    def refresh_asset(self, asset_id: str) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        if asset is None:
            raise ValueError("asset not found")
        candidate = Path(asset["normalized_path"])
        try:
            stat = candidate.stat()
            fingerprint = self._fingerprint(int(stat.st_size), int(stat.st_mtime_ns))
            changed = fingerprint != asset["fingerprint"]
            status = "stale" if changed else asset["status"]
        except OSError:
            fingerprint, changed, status = asset["fingerprint"], True, "missing"
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE project_multimodal_assets SET status=?,updated_at=? WHERE id=?",
                (status, time.time(), asset_id),
            )
            if changed and asset.get("node_id"):
                connection.execute(
                    "UPDATE project_graph_nodes SET validity='stale',updated_at=? WHERE id=?",
                    (time.time(), asset["node_id"]),
                )
            connection.commit()
        return self.get_asset(asset_id)

    def delete_asset(self, asset_id: str) -> bool:
        asset = self.get_asset(asset_id)
        if asset is None:
            return False
        node_id = asset.get("node_id")
        if node_id:
            self.federation.delete_node(str(node_id))
        with closing(self.connect()) as connection:
            connection.execute(
                "DELETE FROM project_multimodal_assets WHERE id=?", (asset_id,)
            )
            connection.commit()
        return True

    def delete_project(self, project_id: str) -> int:
        with closing(self.connect()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM project_multimodal_assets WHERE project_id=?", (project_id,)
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM project_multimodal_assets WHERE project_id=?", (project_id,)
            )
            connection.commit()
        return int(count)


def json_dumps(value: Any) -> str:
    import json
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: Any) -> dict[str, Any]:
    import json
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}
