from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core import context_budget, llm
from pillow_assistant.core.context_budget import (
    ContextBudgetManager,
    TokenCalibrationStore,
    estimate_text_tokens,
    join_context_and_prompt,
    model_profile_key,
)


class ContextBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_manager = context_budget._DEFAULT_MANAGER
        self.original_litellm = sys.modules.get("litellm")

    def tearDown(self) -> None:
        context_budget._DEFAULT_MANAGER = self.original_manager
        if self.original_litellm is None:
            sys.modules.pop("litellm", None)
        else:
            sys.modules["litellm"] = self.original_litellm
        self.temporary.cleanup()

    def manager(self) -> ContextBudgetManager:
        return ContextBudgetManager(TokenCalibrationStore(self.root / "token-calibration.json"))

    def test_agreed_initial_estimate_ratios(self):
        self.assertEqual(estimate_text_tokens("汉" * 100), 55)
        self.assertEqual(estimate_text_tokens(" ".join(["word"] * 100)), 110)
        self.assertEqual(estimate_text_tokens("{}[]:/\\+-=" * 10), 100)

    def test_85_percent_boundary_compacts_and_keeps_current_request(self):
        manager = self.manager()
        messages = [{"role": "system", "content": "You are helpful."}]
        for number in range(5):
            messages.extend([
                {"role": "user", "content": f"旧问题{number}" + "资料" * 500},
                {"role": "assistant", "content": f"旧回答{number}" + "结果" * 500},
            ])
        current = "请回答当前这个问题，不要丢失这句话。"
        messages.append({
            "role": "user",
            "content": join_context_and_prompt("引用资料" * 1200, current),
        })

        result = manager.prepare(
            provider="Custom",
            api_base="https://example.test/v1",
            model="unknown-model",
            messages=messages,
            context_config={"max_input_tokens": 2400, "soft_ratio": 0.85, "target_ratio": 0.72},
        )

        self.assertTrue(result.compacted)
        self.assertGreaterEqual(result.estimate_before.adjusted_tokens, result.limits.soft_input_tokens)
        self.assertLessEqual(result.estimate_after.adjusted_tokens, result.estimate_before.adjusted_tokens)
        self.assertGreater(result.dropped_rounds + result.shortened_messages, 0)
        self.assertIn(current, str(result.messages[-1]["content"]))

    def test_calibration_is_isolated_by_provider_base_url_and_model(self):
        path = self.root / "token-calibration.json"
        store = TokenCalibrationStore(path)
        first = model_profile_key("Custom", "https://one.test/v1", "same-name")
        second = model_profile_key("Custom", "https://two.test/v1", "same-name")

        store.observe(first, base_estimate=1000, actual_input_tokens=1600)

        first_correction, first_uncertainty = store.factors(first)
        second_correction, second_uncertainty = store.factors(second)
        self.assertGreaterEqual(first_correction, 1.6)
        self.assertEqual(first_uncertainty, 1.2)
        self.assertEqual(second_correction, 1.0)
        self.assertEqual(second_uncertainty, 1.3)
        self.assertEqual(TokenCalibrationStore(path).profile(first)["sample_count"], 1)

    async def test_complete_with_tools_uses_usage_for_calibration(self):
        captured = []
        fake = types.ModuleType("litellm")
        fake.get_model_info = lambda _model: {"max_input_tokens": 1200, "max_output_tokens": 200}

        async def acompletion(**kwargs):
            captured.append(kwargs)
            message = SimpleNamespace(content="完成", tool_calls=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(prompt_tokens=777, completion_tokens=10, total_tokens=787),
            )

        fake.acompletion = acompletion
        sys.modules["litellm"] = fake
        manager = self.manager()
        context_budget._DEFAULT_MANAGER = manager

        turn = await llm.complete_with_tools(
            provider="Custom",
            model="demo",
            api_base="https://gateway.test/v1",
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "历史" * 1800},
                {"role": "assistant", "content": "回答" * 1800},
                {"role": "user", "content": "当前问题"},
            ],
            extra={
                "temperature": 0.2,
                "pillow_context": {"soft_ratio": 0.85, "target_ratio": 0.72},
            },
        )

        self.assertEqual(turn.content, "完成")
        self.assertEqual(turn.usage["prompt_tokens"], 777)
        self.assertTrue(turn.context_stats["compacted"])
        self.assertIsNotNone(turn.managed_messages)
        self.assertEqual(turn.managed_messages, captured[0]["messages"])
        self.assertNotIn("pillow_context", captured[0])
        self.assertEqual(captured[0]["temperature"], 0.2)
        key = model_profile_key("Custom", "https://gateway.test/v1", "demo")
        self.assertEqual(manager.calibration.profile(key)["sample_count"], 1)

    async def test_context_length_error_gets_one_emergency_retry(self):
        calls = []
        fake = types.ModuleType("litellm")
        fake.get_model_info = lambda _model: {"max_input_tokens": 20_000}

        async def acompletion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ValueError("maximum context length exceeded")
            message = SimpleNamespace(content="重试成功", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage={"prompt_tokens": 800})

        fake.acompletion = acompletion
        sys.modules["litellm"] = fake
        context_budget._DEFAULT_MANAGER = self.manager()

        turn = await llm.complete_with_tools(
            provider="Custom",
            model="small-in-reality",
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "旧内容" * 1500},
                {"role": "assistant", "content": "旧回答" * 1500},
                {"role": "user", "content": "继续当前任务"},
            ],
        )

        self.assertEqual(turn.content, "重试成功")
        self.assertEqual(len(calls), 2)
        self.assertLess(len(str(calls[1]["messages"])), len(str(calls[0]["messages"])))


if __name__ == "__main__":
    unittest.main()
