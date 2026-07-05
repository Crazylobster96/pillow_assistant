"""R0 smoke + unit tests. Run headless: ``python tests/test_r0.py``.

Covers the non-GUI seam introduced in R0: contracts, LLM helpers, the credential
vault, the DB plaintext-key migration, and the LLM event-bus handler. GUI modules
(PySide6 dialogs) are import-smoke-tested separately by the build script.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType, RequestKind, SurfaceLevel
from pillow_assistant.core import llm
from pillow_assistant.core.handlers import LLMHandler
from storage import Storage, Vault

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_contracts():
    print("contracts")
    r = AppRequest(prompt="hi")
    check("request id auto", isinstance(r.id, str) and len(r.id) == 12)
    check("default kind text", r.kind == RequestKind.TEXT)
    ev = AgentEvent(request_id=r.id, type=EventType.TOKEN, text="x")
    check("event roundtrip", ev.request_id == r.id and ev.text == "x")


def test_llm_helpers():
    print("llm helpers")
    check("openai prefix", llm.resolve_model_string("OpenAI", "gpt-4o") == "openai/gpt-4o")
    check("vllm openai-compat", llm.resolve_model_string("vLLM", "qwen2") == "openai/qwen2")
    check("ollama prefix", llm.resolve_model_string("Ollama", "llama3") == "ollama/llama3")
    check("already-qualified kept", llm.resolve_model_string("OpenAI", "openai/gpt-4o") == "openai/gpt-4o")
    try:
        llm.resolve_model_string("OpenAI", "")
        check("empty model raises", False)
    except ValueError:
        check("empty model raises", True)
    msgs = llm.build_messages("hello")
    check("text message shape", msgs[0]["content"] == "hello")
    check("parse_extra ok", llm.parse_extra('{"temperature":0.5}') == {"temperature": 0.5})
    check("parse_extra garbage", llm.parse_extra("not json") == {})


def test_vault():
    print("vault")
    with tempfile.TemporaryDirectory() as d:
        v = Vault(fallback_path=Path(d) / "secrets.json")
        v.set_secret("My Model", "sk-12345")
        check("get secret", v.get_secret("My Model") == "sk-12345")
        v.delete_secret("My Model")
        check("deleted", v.get_secret("My Model") is None)


def test_db_migration():
    print("db migration (plaintext -> vault)")
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "assistant.db"
        # Build an OLD-schema DB with a plaintext api_key.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE model_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                model_type TEXT NOT NULL, display_name TEXT NOT NULL, base_url TEXT,
                api_key TEXT, extra TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
        )
        conn.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO model_configs(provider, model_type, display_name, base_url, api_key, extra)"
            " VALUES('OpenAI','llm','GPT','https://api.openai.com/v1','sk-PLAINTEXT', NULL)"
        )
        conn.commit()
        conn.close()

        vault = Vault(fallback_path=Path(d) / "secrets.json")
        store = Storage(db_path)
        store.ensure_schema()
        migrated = store.migrate_plaintext_keys(vault)
        check("one key migrated", migrated == 1)

        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_configs)")}
        conn.close()
        check("api_key column dropped", "api_key" not in cols)
        check("model column present", "model" in cols)
        check("secret now in vault", vault.get_secret("GPT") == "sk-PLAINTEXT")

        # No plaintext anywhere in the raw DB bytes.
        raw = Path(db_path).read_bytes()
        check("no plaintext in db file", b"sk-PLAINTEXT" not in raw)

        cfg = store.get_model_config("GPT")
        check("get_model_config works", cfg is not None and cfg["provider"] == "OpenAI")

        # replace_model_configs routes new keys to the vault, not the DB.
        store.replace_model_configs(
            [{"provider": "Ollama", "model_type": "llm", "display_name": "Local",
              "model": "llama3", "base_url": "http://localhost:11434", "api_key": "sk-NEW"}],
            vault,
        )
        raw2 = Path(db_path).read_bytes()
        check("new key not in db", b"sk-NEW" not in raw2)
        check("new key in vault", vault.get_secret("Local") == "sk-NEW")
        check("old vault secret cleaned", vault.get_secret("GPT") is None)


def test_handler():
    print("LLM handler (event stream)")

    async def fake_stream(**kwargs):
        for t in ["Hello", ", ", "world"]:
            yield t

    original = llm.stream_completion
    llm.stream_completion = fake_stream
    try:
        class FakeStore:
            def get_model_config(self, ref):
                return {"display_name": ref, "provider": "OpenAI", "model": "gpt-4o",
                        "base_url": None, "extra": None}

        class FakeVault:
            def get_secret(self, name):
                return "sk-x"

        events = []

        async def emit(ev):
            events.append(ev)

        async def run():
            handler = LLMHandler(FakeStore(), FakeVault())
            await handler(AppRequest(prompt="hi", model_ref="GPT"), emit)

        asyncio.run(run())
        types = [e.type for e in events]
        check("starts with START", types[0] == EventType.START)
        check("has tokens", types.count(EventType.TOKEN) == 3)
        check("ends with DONE", types[-1] == EventType.DONE)
        surface = [e for e in events if e.type == EventType.SURFACE]
        check("surface body assembled", surface and surface[0].surface.body == "Hello, world")
        check("surface level L4", surface and surface[0].surface.level == SurfaceLevel.L4)
    finally:
        llm.stream_completion = original


def test_handler_missing_config():
    print("LLM handler (missing config -> error)")

    class EmptyStore:
        def get_model_config(self, ref):
            return None

    events = []

    async def emit(ev):
        events.append(ev)

    async def run():
        await LLMHandler(EmptyStore(), None)(AppRequest(prompt="x", model_ref="nope"), emit)

    asyncio.run(run())
    check("single error event", len(events) == 1 and events[0].type == EventType.ERROR)


if __name__ == "__main__":
    for t in (test_contracts, test_llm_helpers, test_vault, test_db_migration,
              test_handler, test_handler_missing_config):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
