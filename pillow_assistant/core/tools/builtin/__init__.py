"""Built-in tools and the default manifest-backed registry."""

from __future__ import annotations

from pillow_assistant.capabilities.tool_manifest import ToolManifestRegistry
from pillow_assistant.core.tools.registry import ToolRegistry


def build_default_registry() -> ToolRegistry:
    from pillow_assistant.core.tools.builtin.ask_tool import AskUserTool
    from pillow_assistant.core.tools.builtin.browser_tool import BrowserReadTool
    from pillow_assistant.core.tools.builtin.cli_tool import RunCliTool
    from pillow_assistant.core.tools.builtin.config_tools import (
        AssignModelRoleTool,
        ConfigureModelTool,
        ListModelsTool,
        SetLanguageTool,
        SetMaxStepsTool,
        SetSurfaceTransparencyTool,
    )
    from pillow_assistant.core.tools.builtin.file_tool import FileListTool, FileReadTool, FileWriteTool
    from pillow_assistant.core.tools.builtin.http_tool import HttpRequestTool
    from pillow_assistant.core.tools.builtin.present_tool import PresentTool
    from pillow_assistant.core.tools.builtin.project_memory_tools import RequestProjectMemoryTool
    from pillow_assistant.core.tools.builtin.project_tools import DeleteProjectTool
    from pillow_assistant.core.tools.builtin.python_tool import PythonTool
    from pillow_assistant.core.tools.builtin.video_tool import ProcessVideoTool

    reg = ToolRegistry()
    manifests = ToolManifestRegistry()

    def register(tool) -> None:
        reg.register(manifests.bind(tool))

    register(PythonTool())
    register(FileReadTool())
    register(FileWriteTool())
    register(FileListTool())
    register(HttpRequestTool())
    register(RunCliTool())
    register(BrowserReadTool())
    register(PresentTool())
    register(ListModelsTool())
    register(ConfigureModelTool())
    register(AssignModelRoleTool())
    register(SetLanguageTool())
    register(SetMaxStepsTool())
    register(SetSurfaceTransparencyTool())
    register(AskUserTool())
    register(ProcessVideoTool())
    register(RequestProjectMemoryTool())
    register(DeleteProjectTool())
    return reg
