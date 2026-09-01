"""File-backed prompts, tool manifests, and skill discovery for Pillow Assistant."""

from pillow_assistant.capabilities.prompt_registry import (
    PromptRegistry,
    get_prompt_registry,
    prompt_metadata,
    render_prompt,
)
from pillow_assistant.capabilities.skill_registry import CapabilitySkillRegistry
from pillow_assistant.capabilities.tool_manifest import ToolManifestRegistry

__all__ = [
    "CapabilitySkillRegistry",
    "PromptRegistry",
    "ToolManifestRegistry",
    "get_prompt_registry",
    "prompt_metadata",
    "render_prompt",
]
