"""Tests for the lightweight i18n module."""

import importlib
import os

import pillow_assistant.core.i18n as i18n


def _reload_with(env_lang=None):
    old = os.environ.get("PILLOW_LANG")
    if env_lang is None:
        os.environ.pop("PILLOW_LANG", None)
    else:
        os.environ["PILLOW_LANG"] = env_lang
    try:
        return importlib.reload(i18n)
    finally:
        if old is None:
            os.environ.pop("PILLOW_LANG", None)
        else:
            os.environ["PILLOW_LANG"] = old


def test_env_override_zh():
    mod = _reload_with("zh")
    assert mod.LANG == "zh"
    assert mod.t("menu.quit") == "退出"


def test_env_override_en():
    mod = _reload_with("en")
    assert mod.LANG == "en"
    assert mod.t("menu.quit") == "Quit"


def test_formatting():
    mod = _reload_with("zh")
    assert "report.md" in mod.t("tool.fw.undo_create", rel="report.md")


def test_missing_key_returns_key():
    mod = _reload_with("en")
    assert mod.t("no.such.key") == "no.such.key"


def test_en_pack_covers_zh_keys():
    mod = _reload_with("zh")
    zh, en = mod._ZH, mod._EN
    missing = [k for k in zh if k not in en]
    assert not missing, f"en pack missing keys: {missing}"


def test_bad_format_args_do_not_crash():
    mod = _reload_with("zh")
    # wrong kwargs -> falls back to the raw template instead of raising
    assert mod.t("tool.fw.undo_create", wrong="x")


def teardown_module(module):  # restore the module for other tests
    importlib.reload(i18n)
