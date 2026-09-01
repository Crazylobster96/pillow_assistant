"""Versioned, file-backed prompt templates with strict variable rendering."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Optional

_VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptRegistryError(RuntimeError):
    """Raised when the prompt catalog or a prompt template is invalid."""


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    version: str
    files: dict[str, str]
    variables: tuple[str, ...]
    role: str


class PromptRegistry:
    """Load prompt definitions from package data or an explicit test directory."""

    def __init__(self, root: Optional[str | Path] = None, *, language: Optional[str] = None) -> None:
        self.root = Path(root) if root is not None else resources.files("pillow_assistant.capabilities").joinpath("prompts")
        self.language = language
        self._specs: dict[str, PromptSpec] = {}
        self.reload()

    def _read_text(self, relative: str) -> str:
        target = self.root.joinpath(relative)
        try:
            return target.read_text(encoding="utf-8")
        except TypeError:  # importlib Traversable uses a positional encoding on older Python.
            return target.read_text("utf-8")
        except OSError as exc:
            raise PromptRegistryError(f"Prompt resource is unavailable: {relative}: {exc}") from exc

    def reload(self) -> None:
        try:
            raw = json.loads(self._read_text("catalog.json"))
        except (ValueError, TypeError) as exc:
            raise PromptRegistryError(f"Invalid prompt catalog: {exc}") from exc
        entries = raw.get("prompts") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise PromptRegistryError("Prompt catalog must contain a prompts array")
        specs: dict[str, PromptSpec] = {}
        for item in entries:
            if not isinstance(item, dict):
                raise PromptRegistryError("Every prompt catalog entry must be an object")
            prompt_id = str(item.get("id") or "").strip()
            version = str(item.get("version") or "").strip()
            files = item.get("files")
            variables = item.get("variables") or []
            role = str(item.get("role") or "system")
            if not prompt_id or not version or not isinstance(files, dict) or not files:
                raise PromptRegistryError(f"Incomplete prompt entry: {prompt_id or '<missing id>'}")
            if prompt_id in specs:
                raise PromptRegistryError(f"Duplicate prompt id: {prompt_id}")
            specs[prompt_id] = PromptSpec(
                prompt_id=prompt_id,
                version=version,
                files={str(k): str(v) for k, v in files.items()},
                variables=tuple(str(value) for value in variables),
                role=role,
            )
        self._specs = specs

    def ids(self) -> list[str]:
        return sorted(self._specs)

    def _language(self, requested: Optional[str]) -> str:
        if requested in {"zh", "en"}:
            return str(requested)
        if self.language in {"zh", "en"}:
            return str(self.language)
        from pillow_assistant.core import i18n
        return i18n.LANG if i18n.LANG in {"zh", "en"} else "en"

    def _template(self, spec: PromptSpec, language: Optional[str]) -> tuple[str, str]:
        lang = self._language(language)
        relative = spec.files.get(lang) or spec.files.get("default") or spec.files.get("en") or spec.files.get("zh")
        if not relative:
            raise PromptRegistryError(f"Prompt {spec.prompt_id} has no usable template for {lang}")
        return self._read_text(relative).rstrip("\r\n"), relative

    def render(
        self,
        prompt_id: str,
        *,
        variables: Optional[dict[str, Any]] = None,
        language: Optional[str] = None,
    ) -> str:
        spec = self._specs.get(prompt_id)
        if spec is None:
            raise PromptRegistryError(f"Unknown prompt id: {prompt_id}")
        template, _ = self._template(spec, language)
        values = dict(variables or {})
        required = set(spec.variables)
        referenced = set(_VARIABLE_RE.findall(template))
        missing = sorted((required | referenced) - set(values))
        if missing:
            raise PromptRegistryError(f"Prompt {prompt_id} is missing variables: {', '.join(missing)}")
        unknown = sorted(set(values) - (required | referenced))
        if unknown:
            raise PromptRegistryError(f"Prompt {prompt_id} received unknown variables: {', '.join(unknown)}")

        def replace(match: re.Match[str]) -> str:
            return str(values[match.group(1)])

        return _VARIABLE_RE.sub(replace, template)

    def metadata(self, prompt_id: str, *, language: Optional[str] = None) -> dict[str, Any]:
        spec = self._specs.get(prompt_id)
        if spec is None:
            raise PromptRegistryError(f"Unknown prompt id: {prompt_id}")
        template, relative = self._template(spec, language)
        return {
            "id": spec.prompt_id,
            "version": spec.version,
            "role": spec.role,
            "language": self._language(language),
            "source": relative,
            "sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
            "variables": list(spec.variables),
        }

    def snapshot(self, prompt_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        return [self.metadata(prompt_id) for prompt_id in (prompt_ids or self.ids())]


_DEFAULT_REGISTRY: Optional[PromptRegistry] = None


def get_prompt_registry() -> PromptRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = PromptRegistry()
    return _DEFAULT_REGISTRY


def render_prompt(prompt_id: str, **variables: Any) -> str:
    return get_prompt_registry().render(prompt_id, variables=variables)


def prompt_metadata(prompt_id: str) -> dict[str, Any]:
    return get_prompt_registry().metadata(prompt_id)
