"""Tool contract for the Agent's pluggable tool system (T0).

A Tool exposes a JSON-Schema and an async handler. The handler receives the
parsed args plus a ToolContext (current workspace, session, emit, vault,
referenced paths) and returns a ToolResult. Permissions gate dangerous tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable


class Permission(Enum):
    READONLY = "readonly"    # no side effects
    WRITE_WS = "write_ws"    # writes inside the project workspace
    NETWORK = "network"      # goes out to the network
    SYSTEM = "system"        # system-level / dangerous (confirm or 5s-undo)


Emit = Callable[[Any], Awaitable[None]]


@dataclass
class ToolContext:
    workspace: Path
    session: Any = None
    emit: Optional[Emit] = None
    vault: Any = None
    references: list[str] = field(default_factory=list)
    http_allowlist: Optional[list] = None  # if set, only these domains for http_request
    allow_cli: bool = True                 # gate for the run_cli tool
    audit: Any = None                      # optional AuditLog for tool-call tracing
    undo_manager: Any = None               # optional UndoManager for reversible actions
    request_id: str = ""                   # id of the request this run serves (for emitted events)
    storage: Any = None                    # optional Storage for self-configuration tools
    ask: Any = None                        # optional async callable(spec)->dict to ask the user


@dataclass
class ToolResult:
    ok: bool
    text: str                       # fed back to the model
    artifacts: list[str] = field(default_factory=list)
    undo_token: Optional[str] = None  # if set, the action can be undone (5s window)
    undo_label: str = ""


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: dict
    permission: Permission

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult: ...
