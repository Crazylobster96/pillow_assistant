"""run_python's time limit: 60 s by default, longer when the model asks, capped."""

import asyncio
import json

from pillow_assistant.core.settings import set_setting
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin import python_tool


def test_the_model_can_ask_for_more_time_within_the_cap():
    assert python_tool.timeout_for({}) == 60
    assert python_tool.timeout_for({"timeout": 300}) == 300
    assert python_tool.timeout_for({"timeout": 5000}) == 600
    assert python_tool.timeout_for({"timeout": "abc"}) == 60
    set_setting("python_timeout", 90)
    set_setting("python_timeout_max", 1200)
    assert python_tool.timeout_for({}) == 90 and python_tool.timeout_for({"timeout": 5000}) == 1200


def test_a_timeout_says_how_to_retry(tmp_path):
    result = asyncio.run(python_tool.PythonTool()({"code": "import time\ntime.sleep(30)", "timeout": 5},
                                                  ToolContext(workspace=str(tmp_path))))
    assert not result.ok and "TIMEOUT" in result.text and "600" in result.text


def test_the_schema_offers_timeout():
    from pillow_assistant.core.tools.builtin import build_default_registry

    schema = next(s for s in build_default_registry().schemas() if s["function"]["name"] == "run_python")
    assert schema["function"]["parameters"]["properties"]["timeout"]["type"] == "integer"
    json.dumps(schema)
