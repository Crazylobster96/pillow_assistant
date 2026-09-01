"""Compatibility accessors for the file-backed main Agent prompt."""

from pillow_assistant.capabilities.prompt_registry import prompt_metadata, render_prompt

SYSTEM_PROMPT = render_prompt("shared.main_agent")
SYSTEM_PROMPT_METADATA = prompt_metadata("shared.main_agent")
