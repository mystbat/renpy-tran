"""FEATURE(enable-thinking)：思维链开关的配置层 / provider 层 / UI 层测试。"""
from __future__ import annotations

import configparser
from pathlib import Path
from types import SimpleNamespace

import pytest

from pytran.config import Config, _DEFAULT_INI
from pytran.providers import OpenAIProvider
from pytran.terms import TermProfile


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))])


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def _provider(responses, **cfg_kw):
    client = FakeClient(responses)
    cfg = Config(api_key="sk-test-key", model="m1", **cfg_kw)
    return OpenAIProvider(cfg, client=client, sleep=lambda _s: None), client


# --------------------------------------------------------------------------- #
# 配置层
# --------------------------------------------------------------------------- #
def test_enable_thinking_defaults_to_true():
    """默认 True = 维持既有行为，旧 config.ini 无需迁移。"""
    assert Config().enable_thinking is True


def test_default_ini_template_has_enable_thinking():
    """默认模板必须带上该键，否则首次运行的用户看不到也改不了。"""
    assert "enable_thinking = true" in _DEFAULT_INI


def test_enable_thinking_roundtrips_through_ini(tmp_path: Path):
    """保存为 false → 重新加载仍为 False（不是字符串 "False" 的真值）。"""
    cfg = Config(api_key="k", model="m", enable_thinking=False, work_dir=str(tmp_path))
    cfg.save(tmp_path)
    loaded = Config.load(tmp_path)
    assert loaded.enable_thinking is False


def test_enable_thinking_missing_key_keeps_true(tmp_path: Path):
    """旧 config.ini（无该键）加载后仍为 True——向后兼容。"""
    ini = tmp_path / "config.ini"
    ini.write_text("[Model]\nmodel = m1\ntop_p = 0.9\n", encoding="utf-8")
    assert Config.load(tmp_path).enable_thinking is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("false", False), ("1", True), ("0", False),
    ("yes", True), ("no", False), ("on", True), ("off", False),
])
def test_enable_thinking_accepts_configparser_booleans(tmp_path: Path, raw, expected):
    ini = tmp_path / "config.ini"
    ini.write_text("[Model]\nmodel = m1\nenable_thinking = %s\n" % raw, encoding="utf-8")
    assert Config.load(tmp_path).enable_thinking is expected


# --------------------------------------------------------------------------- #
# Provider 层：只有「关」才发 extra_body，且只管翻译
# --------------------------------------------------------------------------- #
def test_provider_sends_extra_body_when_thinking_disabled():
    provider, client = _provider(['{"i": 1, "t": "你好"}'], enable_thinking=False)
    provider.translate_batch([(1, "Hello")], TermProfile())
    assert client.chat.completions.calls[0]["extra_body"] == {"enable_thinking": False}


def test_provider_omits_extra_body_when_thinking_enabled():
    """True 时不发该参数，保持与历史请求逐字节一致。"""
    provider, client = _provider(['{"i": 1, "t": "你好"}'], enable_thinking=True)
    provider.translate_batch([(1, "Hello")], TermProfile())
    assert "extra_body" not in client.chat.completions.calls[0]


def test_provider_omits_extra_body_when_attr_missing():
    """config 对象没有该属性时（测试替身/旧对象）按 True 处理，不抛异常。"""
    provider, client = _provider(['{"i": 1, "t": "你好"}'])
    del provider._config.enable_thinking
    provider.translate_batch([(1, "Hello")], TermProfile())
    assert "extra_body" not in client.chat.completions.calls[0]


def test_review_ignores_enable_thinking():
    """用户明确要求：开关只管翻译请求，复核不受影响。"""
    provider, client = _provider(["OK"], enable_thinking=False)
    provider.review_text("prompt")
    call = client.chat.completions.calls[0]
    assert "extra_body" not in call
