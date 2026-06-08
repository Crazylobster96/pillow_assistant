"""Built-in tools and the default registry (T0)."""

from __future__ import annotations

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
    )
    from pillow_assistant.core.tools.builtin.file_tool import FileListTool, FileReadTool, FileWriteTool
    from pillow_assistant.core.tools.builtin.http_tool import HttpRequestTool
    from pillow_assistant.core.tools.builtin.present_tool import PresentTool
    from pillow_assistant.core.tools.builtin.python_tool import PythonTool
    from pillow_assistant.core.tools.builtin.video_tool import ProcessVideoTool

    reg = ToolRegistry()
    reg.register(PythonTool())
    reg.register(FileReadTool())
    reg.register(FileWriteTool())
    reg.register(FileListTool())
    reg.register(HttpRequestTool())
    reg.register(RunCliTool())
    reg.register(BrowserReadTool())
    reg.register(PresentTool())
    reg.register(ListModelsTool())
    reg.register(ConfigureModelTool())
    reg.register(AssignModelRoleTool())
   