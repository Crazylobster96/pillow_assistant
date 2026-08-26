"""Backend capability contract shared by all project-memory layers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class BackendCapabilityError(ValueError):
    pass


@dataclass(frozen=True)
class BackendCapabilities:
    backend_id: str
    contract_version: str = "1"
    authoritative_state: bool = False
    task_validation: bool = False
    resume: bool = False
    source_references: bool = False
    keyword_search: bool = False
    vector_search: bool = False
    metadata_filter: bool = False
    delete_project: bool = False

    def enabled(self) -> set[str]:
        values = asdict(self)
        return {key for key, value in values.items() if isinstance(value, bool) and value}


CONTROL_CAPABILITIES = {
    "authoritative_state", "task_validation", "resume", "source_references", "delete_project",
}
RETRIEVAL_CAPABILITIES = {"keyword_search", "vector_search", "metadata_filter"}


def infer_backend_capabilities(backend: Any, backend_id: str = "duck-typed") -> BackendCapabilities:
    declared = getattr(backend, "capabilities", None)
    if isinstance(declared, BackendCapabilities):
        return declared
    return BackendCapabilities(
        backend_id=backend_id,
        authoritative_state=all(hasattr(backend, name) for name in ("get_state", "update_state")),
        task_validation=all(hasattr(backend, name) for name in (
            "create_task", "record_validation_result", "evaluate_task_completion",
        )),
        resume=all(hasattr(backend, name) for name in ("save_resume", "load_resume", "clear_resume")),
        source_references=all(hasattr(backend, name) for name in ("register_source", "refresh_source")),
        keyword_search=hasattr(backend, "search_memory"),
        vector_search=False,
        metadata_filter=hasattr(backend, "search_memory"),
        delete_project=hasattr(backend, "delete_project_memory"),
    )


def validate_backend_capabilities(capabilities: BackendCapabilities, mode: str) -> None:
    selected = str(mode or "disabled").lower()
    if selected == "disabled":
        return
    if selected not in {"augment", "replace"}:
        raise BackendCapabilityError(f"unsupported project-memory backend mode: {mode}")
    enabled = capabilities.enabled()
    if selected == "augment":
        if not enabled.intersection({"keyword_search", "vector_search"}):
            raise BackendCapabilityError("augment mode requires keyword_search or vector_search")
        return
    missing = sorted((CONTROL_CAPABILITIES | RETRIEVAL_CAPABILITIES) - enabled)
    if missing:
        raise BackendCapabilityError(
            "replace mode backend is incomplete; missing capabilities: " + ", ".join(missing)
        )
