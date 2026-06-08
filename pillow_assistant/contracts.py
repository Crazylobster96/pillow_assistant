"""Frozen contracts shared between the UI shell and the execution core.

These pydantic models are the seam introduced in refactor stage R0: the UI emits
``AppRequest`` onto the event bus and renders the ``AgentEvent`` stream it gets
back. Keeping them stable lets the front-end and the core evolve in parallel.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def new_id() -> str:
    """Short, unique identifier for correlating requests with their events."""
    return uuid.uuid4().hex[:12]


class RequestKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"


class AppRequest(BaseModel):
    """A single user request flowing from the UI into the execution core."""

    id: str = Field(default_factory=new_id)
    kind: RequestKind = RequestKind.TEXT
    prompt: str = ""
    model_ref: Optional[str] = None  # display_name of the chosen model config
    image_path: Optional[str] = None
    project_id: Optional[str] = None
    # Referenced files/folders for this request: absolute paths only, never copied.
    references: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class SurfaceLevel(str, Enum):
    """Response surface levels L0-L5 (see needs doc 4.5)."""

    L0 = "L0"  # silent
    L1 = "L1"  # micro hint
    L2 = "L2"  # voice
    L3 = "L3"  # edge float
    L4 = "L4"  # card
    L5 = "L5"  # main window


class SurfaceSpec(BaseModel):
    """How a result should be presented. R0 only emits L4 text cards."""

    level: SurfaceLevel = SurfaceLevel.L4
    kind: str = "text"  # text | image | chart | table | code | file
    title: Optional[str] = None
    body: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class EventType(str, Enum):
    START = "start"
    TOKEN = "token"
    SURFACE = "surface"
    DONE = "done"
    ERROR = "error"
    UNDO = "undo"  # offers a 5-second undo for a reversible action
    ASK = "ask"    # agent asks the user a question and waits for the answer


class AgentEvent(BaseModel):
    """A single event streamed from the core back to the UI."""

    request_id: str
    type: EventType
    text: str = ""
    surface: Optional[SurfaceSpec] = None
    meta: dict[str, Any] = Field(default_factory=dict)
