"""File-backed metadata for built-in tools.

Execution and permission enforcement stay in Python.  A manifest may describe a
permission, but it cannot change it: binding fails unless the implementation's
permission declaration matches the manifest exactly.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Optional


class ToolManifestError(RuntimeError):
    """Raised when built-in tool metadata is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class ToolManifest:
    name: str
    version: str
    descriptions: dict[str, str]
    parameters: dict[str, dict[str, Any]]
    permission: str
    modes: tuple[str, ...]
    source: str

    def description(self, language: str) -> str:
        return self.descriptions.get(language) or self.descriptions.get("en") or self.descriptions.get("zh") or ""

    def schema(self, language: str) -> dict[str, Any]:
        value = self.parameters.get(language) or self.parameters.get("en") or self.parameters.get("zh")
        return copy.deepcopy(value or {"type": "object", "properties": {}})


class ToolManifestRegistry:
    def __init__(self, root: Optional[str | Path] = None, *, language: Optional[str] = None) -> None:
        self.root = Path(root) if root is not None else resources.files("pillow_assistant.capabilities").joinpath("tools")
        self.language = language
        self._manifests: dict[str, ToolManifest] = {}
        self.reload()

    def _language(self) -> str:
        if self.language in {"zh", "en"}:
            return str(self.language)
        from pillow_assistant.core import i18n
        return i18n.LANG if i18n.LANG in {"zh", "en"} else "en"

    def reload(self) -> None:
        manifests: dict[str, ToolManifest] = {}
        try:
            children = sorted(self.root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ToolManifestError(f"Tool manifest directory is unavailable: {exc}") from exc
        for child in children:
            if not child.is_dir():
                continue
            target = child.joinpath("manifest.json")
            if not target.is_file():
                continue
            try:
                try:
                    data = json.loads(target.read_text(encoding="utf-8"))
                except TypeError:
                    data = json.loads(target.read_text("utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ToolManifestError(f"Invalid tool manifest {target}: {exc}") from exc
            name = str(data.get("name") or "").strip()
            version = str(data.get("version") or "").strip()
            permission = str(data.get("permission") or "").strip()
            descriptions = data.get("description")
            parameters = data.get("parameters")
            modes = tuple(str(mode) for mode in (data.get("modes") or []))
            if not name or not version or permission not in {"readonly", "write_ws", "network", "system"}:
                raise ToolManifestError(f"Incomplete tool manifest: {target}")
            if not isinstance(descriptions, dict) or not isinstance(parameters, dict):
                raise ToolManifestError(f"Tool manifest must localize description and parameters: {target}")
            if not modes or any(mode not in {"chat", "project"} for mode in modes):
                raise ToolManifestError(f"Invalid tool modes in {target}")
            if name in manifests:
                raise ToolManifestError(f"Duplicate tool manifest: {name}")
            manifests[name] = ToolManifest(
                name=name,
                version=version,
                descriptions={str(k): str(v) for k, v in descriptions.items()},
                parameters={str(k): dict(v) for k, v in parameters.items() if isinstance(v, dict)},
                permission=permission,
                modes=modes,
                source=str(target),
            )
        self._manifests = manifests

    def names(self, mode: Optional[str] = None) -> list[str]:
        return [name for name, item in self._manifests.items() if mode is None or mode in item.modes]

    def get(self, name: str) -> ToolManifest:
        try:
            return self._manifests[name]
        except KeyError as exc:
            raise ToolManifestError(f"No manifest registered for built-in tool: {name}") from exc

    def bind(self, tool: Any) -> Any:
        manifest = self.get(str(getattr(tool, "name", "")))
        declared = getattr(getattr(tool, "permission", None), "value", getattr(tool, "permission", None))
        if declared != manifest.permission:
            raise ToolManifestError(
                f"Permission mismatch for {manifest.name}: implementation={declared!r}, manifest={manifest.permission!r}"
            )
        language = self._language()
        tool.description = manifest.description(language)
        tool.parameters = manifest.schema(language)
        tool.capability_version = manifest.version
        tool.capability_source = manifest.source
        tool.capability_modes = manifest.modes
        return tool

    def snapshot(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        language = self._language()
        return [
            {
                "name": item.name,
                "version": item.version,
                "permission": item.permission,
                "modes": list(item.modes),
                "source": item.source,
                "language": language,
            }
            for item in self._manifests.values()
            if mode is None or mode in item.modes
        ]

_DEFAULT_REGISTRY: Optional[ToolManifestRegistry] = None


def get_tool_manifest_registry() -> ToolManifestRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ToolManifestRegistry()
    return _DEFAULT_REGISTRY


def manifest_tool(cls):
    """Class decorator that supplies prompt-visible metadata from manifest.json."""
    registry = get_tool_manifest_registry()
    manifest = registry.get(str(getattr(cls, "name", "")))
    declared = getattr(getattr(cls, "permission", None), "value", getattr(cls, "permission", None))
    if declared != manifest.permission:
        raise ToolManifestError(
            f"Permission mismatch for {manifest.name}: implementation={declared!r}, manifest={manifest.permission!r}"
        )
    language = registry._language()
    cls.description = manifest.description(language)
    cls.parameters = manifest.schema(language)
    cls.capability_version = manifest.version
    cls.capability_source = manifest.source
    cls.capability_modes = manifest.modes
    return cls
