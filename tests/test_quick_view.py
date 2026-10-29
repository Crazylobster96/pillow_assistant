""""Show me the result" opens the newest outputs at once, without an agent run."""

import os
import time

from pillow_assistant.core import quick_view


def test_only_looking_requests_take_the_shortcut():
    for prompt in ("看下结果", "打开看看", "再打开结果看一下", "展示一下模型", "看看效果"):
        assert quick_view.asks_to_view(prompt), prompt
    for prompt in ("看下结果，再把孔改大一点", "打开 STEP 文件改一下尺寸", "做一个安装板", "结果不对，看看哪里错了"):
        assert not quick_view.asks_to_view(prompt), prompt


def test_the_newest_output_of_each_kind_is_shown(tmp_path):
    def touch(rel, age):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", "utf-8")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    touch("STEP/old.step", 300)
    new_step = touch("STEP/plate.step", 10)
    drawing = touch("DRAWINGS/plate.pdf", 20)
    figure = touch("anatomy/figures/vice/vice.html", 5)
    touch("anatomy/kit/demo.html", 1)  # the skill's own files are not results
    touch("AppData/Local/uv/cache/x/back.pdf", 0)  # nor a tool's cache
    touch("anatomy/shots/_check/idle.png", 0)  # a check screenshot is not a result either
    assert quick_view.latest(tmp_path) == [figure, new_step, drawing]
    chart = touch("charts/sales.png", 2)
    assert quick_view.latest(tmp_path) == [chart, figure, new_step]  # any kind: the three newest


def test_the_sandbox_keeps_uvs_own_cache(tmp_path, monkeypatch):
    from pillow_assistant.core.tools import sandbox

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    box = sandbox.Sandbox(tmp_path / "ws")
    result = box.run_python("import os; print(os.environ.get('UV_CACHE_DIR'))")
    assert str(tmp_path / "local" / "uv" / "cache") in result.stdout
