from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core import context_budget, llm, semantic_context
from pillow_assistant.core.context_budget import ContextBudgetManager, TokenCalibrationStore


def capsule(source_id: str = "message:1") -> dict:
    return {
        "current_goal": "继续完成当前任务",
        "requirements": [
            {"text": "必须保留 C:/repo/app.py", "source_ids": [source_id]},
        ],
        "decisions": [],
        "facts": [],
        "artifacts": [
            {"text": "C:/repo/app.py", "source_ids": [source_id]},
        ],
        "tool_state": [],
        "open_questions": [],
        "exact_fragments": [
            {"text": "C:/repo/app.py", "source_ids": [source_id]},
        ],
        "conflicts": [],
        "source_ids": [source_id],
    }


class SemanticContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        calibration = TokenCalibrationStore(Path(self.temp.name) / "calibration.json")
        self.manager = ContextBudgetManager(calibration)

    def tearDown(self) -> None:
        context_budget._DEFAULT_MANAGER = None
        semantic_context._DEFAULT_COORDINATOR = None
        sys.modules.pop("litellm", None)
        self.temp.cleanup()

    def messages(self, old_size: int = 6_000) -> list[dict]:
        return [
            {"role": "system", "content": "SYSTEM"},
            {
                "role": "user",
                "content": "必须保留 C:/repo/app.py。" + "旧项目资料" * old_size,
            },
            {"role": "assistant", "content": "已记录路径和约束。"},
            {
                "role": "user",
                "content": context_budget.join_context_and_prompt(
                    "引用材料" * 1_000,
                    "请继续当前任务，不要改变文件路径。",
                ),
            },
        ]

    def test_source_split_preserves_active_request(self):
        source = semantic_context.build_semantic_source(self.messages())
        self.assertIsNotNone(source)
        assert source is not None
        self.assertIn("C:/repo/app.py", source.source_text)
        self.assertIn("引用材料", source.source_text)
        self.assertEqual(source.active_request, "请继续当前任务，不要改变文件路径。")

        rebuilt = source.apply(capsule(), verified=True)
        rendered = json.dumps(rebuilt, ensure_ascii=False)
        self.assertIn(semantic_context.CAPSULE_OPEN, rendered)
        self.assertIn("请继续当前任务，不要改变文件路径。", rendered)
        self.assertNotIn("旧项目资料旧项目资料", rendered)
        self.assertNotIn("引用材料引用材料", rendered)

    def test_large_active_tool_result_becomes_source_without_breaking_protocol(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "请分析工具结果"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "tc1", "type": "function",
                    "function": {"name": "scan", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "关键错误 E123\n" + "日志" * 5_000},
        ]
        source = semantic_context.build_semantic_source(messages)
        self.assertIsNotNone(source)
        assert source is not None
        self.assertIn("关键错误 E123", source.source_text)
        self.assertEqual(source.active_request, "请分析工具结果")
        tool_message = source.active_messages[-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "tc1")
        self.assertIn("source_id=message:3:tool-result", tool_message["content"])
        self.assertNotIn("日志日志日志", tool_message["content"])

    def test_capsule_requires_at_least_one_valid_source_reference(self):
        source = semantic_context.build_semantic_source(self.messages())
        self.assertIsNotNone(source)
        assert source is not None
        unsupported = capsule("unknown-source")
        unsupported["current_goal"] = "看似合理但没有来源的目标"
        validated = semantic_context._validate_capsule(unsupported, source)
        self.assertFalse(
            semantic_context._has_provenance(validated, source.source_ids)
        )

    async def test_background_candidate_is_prepared_before_switch(self):
        coordinator = semantic_context.SemanticContextCoordinator()
        calls = []

        async def completion(messages, _max_tokens):
            calls.append(messages)
            if "CONTEXT_VERIFIER" in messages[0]["content"]:
                return '{"valid":true,"missing_critical":[],"distortions":[],"corrected_capsule":null}'
            return json.dumps(capsule(), ensure_ascii=False)

        config = {
            "max_input_tokens": 10_000,
            "semantic_compression": {
                "enabled": True,
                "min_context_tokens": 1,
                "min_source_tokens": 1,
                "background_start_ratio": 0.10,
                "prepare_ratio": 0.50,
                "switch_ratio": 0.85,
                "chunk_tokens": 20_000,
                "verify": True,
            },
        }
        messages = self.messages(old_size=500)
        first = await coordinator.manage(
            manager=self.manager,
            provider="OpenAI",
            api_base=None,
            model="large",
            messages=messages,
            tools=None,
            model_info={"max_input_tokens": 10_000},
            context_config=config,
            completion_extra={},
            compressor_key="compressor",
            completion_call=completion,
        )
        self.assertIn(first.semantic_status, {"background-preparing", "candidate-preparing"})
        self.assertFalse(first.semantic_compressed)

        for _ in range(20):
            await asyncio.sleep(0)
            if len(calls) >= 2:
                break
        for _ in range(5):
            await asyncio.sleep(0)
        second = await coordinator.manage(
            manager=self.manager,
            provider="OpenAI",
            api_base=None,
            model="large",
            messages=messages,
            tools=None,
            model_info={"max_input_tokens": 10_000},
            context_config=config,
            completion_extra={},
            compressor_key="compressor",
            completion_call=completion,
        )
        self.assertEqual(second.semantic_status, "candidate-ready")
        self.assertEqual(len(calls), 2)

    async def test_failed_semantic_verification_falls_back_to_deterministic(self):
        coordinator = semantic_context.SemanticContextCoordinator()

        async def completion(messages, _max_tokens):
            if "CONTEXT_VERIFIER" in messages[0]["content"]:
                return '{"valid":false,"missing_critical":["requirement"],"distortions":[],"corrected_capsule":null}'
            return json.dumps(capsule(), ensure_ascii=False)

        result = await coordinator.manage(
            manager=self.manager,
            provider="OpenAI",
            api_base=None,
            model="large",
            messages=self.messages(),
            tools=None,
            model_info={"max_input_tokens": 8_000},
            context_config={
                "max_input_tokens": 8_000,
                "semantic_compression": {
                    "enabled": True,
                    "min_context_tokens": 1,
                    "min_source_tokens": 1,
                    "background_start_ratio": 0.10,
                    "prepare_ratio": 0.12,
                    "switch_ratio": 0.14,
                    "chunk_tokens": 50_000,
                    "verify": True,
                },
            },
            completion_extra={},
            compressor_key="compressor",
            completion_call=completion,
        )

        self.assertEqual(result.semantic_status, "fallback-deterministic")
        self.assertFalse(result.semantic_compressed)
        self.assertTrue(result.compacted)
        self.assertNotIn(
            semantic_context.CAPSULE_OPEN,
            json.dumps(result.messages, ensure_ascii=False),
        )

    async def test_llm_switches_to_verified_capsule_and_uses_compressor_profile(self):
        calls: list[dict] = []
        fake = types.ModuleType("litellm")
        fake.get_model_info = lambda model: (
            {"max_input_tokens": 50_000, "max_output_tokens": 2_000}
            if "compressor-model" in model else {"max_input_tokens": 8_000, "max_output_tokens": 1_000}
        )

        async def acompletion(**kwargs):
            calls.append(kwargs)
            system = kwargs["messages"][0]["content"]
            if "CONTEXT_COMPRESSOR" in system:
                content = json.dumps(capsule(), ensure_ascii=False)
            elif "CONTEXT_VERIFIER" in system:
                content = '{"valid":true,"missing_critical":[],"distortions":[],"corrected_capsule":null}'
            else:
                content = "主模型完成"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))],
                usage={"prompt_tokens": 900, "completion_tokens": 20},
            )

        fake.acompletion = acompletion
        sys.modules["litellm"] = fake
        context_budget._DEFAULT_MANAGER = self.manager
        semantic_context._DEFAULT_COORDINATOR = semantic_context.SemanticContextCoordinator()

        turn = await llm.complete_with_tools(
            provider="OpenAI",
            model="main-model",
            api_key="main-secret",
            messages=self.messages(),
            extra={
                "temperature": 0.2,
                "pillow_context": {
                    "max_input_tokens": 8_000,
                    "semantic_compression": {
                        "enabled": True,
                        "min_context_tokens": 1,
                        "min_source_tokens": 1,
                        "background_start_ratio": 0.10,
                        "prepare_ratio": 0.12,
                        "switch_ratio": 0.14,
                        "chunk_tokens": 50_000,
                        "verify": True,
                    },
                },
            },
            semantic_profile={
                "provider": "Anthropic",
                "model": "compressor-model",
                "api_base": "https://compressor.test/v1",
                "api_key": "compressor-secret",
                "extra": {"pillow_context": {"ignored": True}},
            },
        )

        self.assertEqual(turn.content, "主模型完成")
        self.assertTrue(turn.context_stats["semantic_compressed"])
        self.assertTrue(turn.context_stats["semantic_verified"])
        self.assertEqual(turn.context_stats["semantic_status"], "verified-capsule")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["model"], "anthropic/compressor-model")
        self.assertEqual(calls[0]["api_key"], "compressor-secret")
        self.assertEqual(calls[1]["api_key"], "compressor-secret")
        self.assertEqual(calls[2]["model"], "openai/main-model")
        self.assertEqual(calls[2]["api_key"], "main-secret")
        self.assertEqual(calls[2]["temperature"], 0.2)
        self.assertNotIn("pillow_context", calls[2])
        self.assertIn("请继续当前任务，不要改变文件路径。", calls[0]["messages"][1]["content"])
        main_messages = json.dumps(calls[2]["messages"], ensure_ascii=False)
        self.assertIn(semantic_context.CAPSULE_OPEN, main_messages)
        self.assertIn("请继续当前任务，不要改变文件路径。", main_messages)
        self.assertNotIn("旧项目资料旧项目资料", main_messages)

    async def test_agent_keeps_raw_transcript_after_managed_model_view(self):
        from pillow_assistant.core.agent.loop import ToolLoopAgent
        from pillow_assistant.core.tools.base import ToolContext, ToolResult

        class Registry:
            def schemas(self):
                return []

            async def dispatch(self, name, args, ctx):
                return ToolResult(ok=True, text="RAW-TOOL-RESULT")

        captured: list[list[dict]] = []

        async def fake_complete_with_tools(**kwargs):
            captured.append(json.loads(json.dumps(kwargs["messages"], ensure_ascii=False)))
            if len(captured) == 1:
                return llm.ToolTurn(
                    tool_calls=[llm.ToolCall(id="call-1", name="scan", arguments="{}")],
                    managed_messages=[
                        {"role": "system", "content": "managed"},
                        {"role": "user", "content": "COMPRESSED-MODEL-VIEW"},
                    ],
                )
            return llm.ToolTurn(
                content="done",
                managed_messages=[{"role": "system", "content": "managed-again"}],
            )

        async def emit(_event):
            return None

        agent = ToolLoopAgent(
            {"provider": "OpenAI", "model": "main-model"},
            "main-secret",
            Registry(),
            ToolContext(workspace=Path(self.temp.name)),
            max_steps=2,
        )
        with patch.object(llm, "complete_with_tools", side_effect=fake_complete_with_tools):
            result = await agent.run(
                prompt="RAW-USER-CONTEXT",
                emit=emit,
                request_id="request-1",
            )

        self.assertEqual(result, "done")
        self.assertEqual(len(captured), 2)
        second_request = json.dumps(captured[1], ensure_ascii=False)
        self.assertIn("RAW-USER-CONTEXT", second_request)
        self.assertIn("RAW-TOOL-RESULT", second_request)
        self.assertNotIn("COMPRESSED-MODEL-VIEW", second_request)
        self.assertIn("RAW-TOOL-RESULT", json.dumps(agent.final_messages, ensure_ascii=False))
        self.assertEqual(agent.last_managed_messages[0]["content"], "managed-again")

    async def test_auxiliary_llm_entries_mark_supporting_and_current_request(self):
        import importlib

        from pillow_assistant.core.conversation_memory import ConversationRouter, ConversationWriteback

        triage_module = importlib.import_module("pillow_assistant.core.triage")

        class Store:
            def list_recent_topics(self, _limit):
                return [{
                    "id": "topic-1", "title": "旧话题", "summary": "历史摘要",
                    "keywords": ["历史"], "last_message_at": 1,
                }]

        captured: list[str] = []

        async def fake_complete(**kwargs):
            captured.append(kwargs["messages"][-1]["content"])
            system = kwargs["messages"][0]["content"]
            if "topic router" in system:
                return '{"kind":"existing_topic","topic_id":"topic-1","confidence":0.9}'
            if "Summarize" in system:
                return '{}'
            if "memory signals" in system:
                return '[]'
            return '{"action":"chat","confidence":0.9}'

        cfg = {"provider": "OpenAI", "model": "test-model"}
        with patch.object(llm, "complete", side_effect=fake_complete):
            await ConversationRouter(Store()).route("继续旧话题", cfg=cfg, api_key=None)
            writeback = ConversationWriteback(None)
            await writeback._summarize("用户长输入", "助手长回答", cfg=cfg, api_key=None)
            await writeback._extract_signals("以后总是简洁回答", "好的", None, cfg=cfg, api_key=None)
            await triage_module.triage(
                "继续项目一", [{"id": "project-1", "name": "项目一"}],
                cfg=cfg, api_key=None,
            )

        self.assertEqual(len(captured), 4)
        for content in captured:
            self.assertIn(context_budget.CONTEXT_OPEN, content)
            self.assertIn(context_budget.REQUEST_OPEN, content)
        route_request = captured[0].split(context_budget.REQUEST_OPEN, 1)[1]
        triage_request = captured[3].split(context_budget.REQUEST_OPEN, 1)[1]
        self.assertIn("继续旧话题", route_request)
        self.assertIn("继续项目一", triage_request)

    def test_compression_profile_resolves_from_secure_model_config(self):
        class Store:
            def get_model_config(self, ref):
                self.ref = ref
                return {
                    "display_name": "压缩模型",
                    "provider": "OpenAI",
                    "model": "small-compressor",
                    "base_url": "https://compressor.test/v1",
                    "extra": '{"temperature":0.1}',
                }

        class Vault:
            def get_secret(self, name):
                return f"secret-for-{name}"

        store = Store()
        profile = semantic_context.resolve_compression_profile(
            store,
            Vault(),
            {
                "extra": json.dumps({
                    "pillow_context": {
                        "semantic_compression": {"model_ref": "压缩模型"},
                    }
                }, ensure_ascii=False),
            },
        )
        self.assertEqual(store.ref, "压缩模型")
        self.assertEqual(profile["model"], "small-compressor")
        self.assertEqual(profile["api_key"], "secret-for-压缩模型")

    def test_compressor_chunk_size_is_capped_to_its_window(self):
        configured = {
            "semantic_compression": {
                "enabled": True,
                "chunk_tokens": 24_000,
            }
        }
        capped = llm._cap_semantic_chunk_size(
            configured, {"max_input_tokens": 8_000, "max_output_tokens": 1_000}
        )
        self.assertEqual(capped["semantic_compression"]["chunk_tokens"], 4_400)
        self.assertEqual(capped["semantic_compression"]["summary_max_tokens"], 1_000)
        self.assertEqual(configured["semantic_compression"]["chunk_tokens"], 24_000)

    async def test_compression_model_role_can_be_assigned(self):
        from pillow_assistant.core import model_roles
        from pillow_assistant.core.tools.base import ToolContext
        from pillow_assistant.core.tools.builtin.config_tools import AssignModelRoleTool

        class Store:
            def list_model_configs(self):
                return [
                    {
                        "display_name": "压缩模型",
                        "provider": "OpenAI",
                        "model": "small-compressor",
                    }
                ]

        roles_path = Path(self.temp.name) / "roles.json"
        with patch.object(model_roles, "roles_path", lambda: roles_path):
            result = await AssignModelRoleTool()(
                {"role": "compression", "model": "压缩模型"},
                ToolContext(workspace=Path(self.temp.name), storage=Store()),
            )
            self.assertTrue(result.ok)
            self.assertEqual(model_roles.load_roles()["compression"], "压缩模型")


if __name__ == "__main__":
    unittest.main()
