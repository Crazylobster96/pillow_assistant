"""Tool calls a model wrote as text instead of making them.

Some models (qwen through OpenAI-compatible endpoints, now and then) put the
call in the message body, e.g.::

    [tool.run_python]
    <parameter name="code">print(1)</parameter>
    </invoke>

The endpoint then reports no tool call, and the loop used to take the text as
the final answer: the task ended with nothing done. ``recover`` turns such text
back into calls when the tool and its arguments can be read; ``looks_like_call``
spots the ones it cannot read, so the loop can ask the model to call again.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

# Where a call starts: [tool.NAME], <invoke name="NAME">, <function=NAME>, <tool_call>{json}
_START = re.compile(
    r"\[tool\.(?P<a>[\w-]+)\]"
    r"|<invoke\s+name=[\"'](?P<b>[\w-]+)[\"']\s*>"
    r"|<function=(?P<c>[\w-]+)>"
    r"|<tool_call>\s*(?P<json>\{.*?\})\s*(?:</tool_call>|$)",
    re.S,
)
_PARAM = re.compile(
    r"<parameter(?:\s+name=[\"'](?P<n1>[\w-]+)[\"']|=(?P<n2>[\w-]+))\s*>\n?(?P<v>.*?)\n?(?:</parameter>|(?=<parameter[\s=])|$)",
    re.S,
)
_END = re.compile(r"</invoke>|</function>|</tool_call>")
_HINT = re.compile(r"\[tool\.[\w-]+\]|<invoke\s+name=|<function=[\w-]+>|<tool_call>|<parameter[\s=]")


@dataclass
class TextCall:
    id: str
    name: str
    arguments: str  # JSON, like an endpoint's tool call


def looks_like_call(text: str) -> bool:
    return bool(text) and _HINT.search(text) is not None


def _typed(value: str, schema: dict) -> Any:
    if schema.get("type") in (None, "string"):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def recover(text: str, tools: list[dict]) -> Optional[tuple[str, list[TextCall]]]:
    """(text before the first call, calls) when every call names an offered tool, else None."""
    if not looks_like_call(text):
        return None
    schemas = {t.get("function", {}).get("name"): (t.get("function", {}).get("parameters") or {}) for t in tools}
    starts = list(_START.finditer(text))
    if not starts:
        return None
    calls = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        body = text[match.end():end]
        if match.group("json"):
            try:
                data = json.loads(match.group("json"))
            except ValueError:
                return None
            name, args = data.get("name"), data.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    return None
        else:
            name = match.group("a") or match.group("b") or match.group("c")
            closing = _END.search(body)
            body = body[:closing.start()] if closing else body
            props = (schemas.get(name) or {}).get("properties") or {}
            args = {}
            for param in _PARAM.finditer(body):
                key = param.group("n1") or param.group("n2")
                args[key] = _typed(param.group("v"), props.get(key) or {})
        if name not in schemas or not isinstance(args, dict):
            return None
        missing = [k for k in (schemas[name].get("required") or []) if k not in args]
        if missing:
            return None
        calls.append(TextCall(id=f"text_call_{i + 1}", name=name, arguments=json.dumps(args, ensure_ascii=False)))
    return text[:starts[0].start()].rstrip(), calls
