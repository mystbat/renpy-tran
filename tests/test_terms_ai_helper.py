"""W5c：``OpenAIProvider.complete_text`` 与 config 新字段（§2 信号源 C / §4）。

``complete_text`` 是信号 C 的轻量通道：不走 JSONL 协议、单次不重试、
``max_tokens`` 收紧到 512、异常统一转 ``ProviderError``（由
``terms._ai_candidates`` 吞掉，不让它成为抽取失败的原因）。
"""

from __future__ import annotations

import pytest

from pytran.config import Config
from pytran.interfaces import ProviderError
from pytran.providers import (
    TERM_EXTRACT_MAX_TOKENS,
    TERM_EXTRACT_TIMEOUT,
    OpenAIProvider,
)


class _Resp:
    """最小 OpenAI 响应替身（属性访问形态）。"""

    class _Choice:
        class message:  # noqa: D106
            content = "Horos\nLena\nStalkfap"

    choices = [_Choice]


class _FakeCompletions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **params):
        self.owner.captured = params
        return _Resp()


class _FakeClient:
    def __init__(self):
        self.captured = None
        self.chat = type("C", (), {})()
        self.chat.completions = _FakeCompletions(self)


class _BoomClient:
    class chat:  # noqa: N801
        class completions:  # noqa: N801
            @staticmethod
            def create(**params):
                raise TimeoutError("slow")


def _cfg(**kw) -> Config:
    kw.setdefault("api_key", "test-key")
    kw.setdefault("model", "gpt-x")
    return Config(**kw)


def test_complete_text_returns_plain_text() -> None:
    """基本调用：返回纯文本，不走 JSONL 解析。"""
    client = _FakeClient()
    p = OpenAIProvider(_cfg(), client=client)
    assert p.complete_text("列出人名") == "Horos\nLena\nStalkfap"


def test_complete_text_params_are_tightened() -> None:
    """max_tokens/timeout 按信号 C 口径收紧，不继承 translate_batch 的大值。"""
    client = _FakeClient()
    p = OpenAIProvider(_cfg(max_tokens=4096, timeout=300.0), client=client)
    p.complete_text("x")
    assert client.captured["max_tokens"] == TERM_EXTRACT_MAX_TOKENS == 512
    assert client.captured["timeout"] == TERM_EXTRACT_TIMEOUT == 60.0


def test_complete_text_includes_system_when_given() -> None:
    """system 非空时进 messages；为空则不塞 system 消息。"""
    client = _FakeClient()
    p = OpenAIProvider(_cfg(), client=client)
    p.complete_text("prompt", system="只输出名字")
    msgs = client.captured["messages"]
    assert msgs[0] == {"role": "system", "content": "只输出名字"}
    assert msgs[-1] == {"role": "user", "content": "prompt"}

    p.complete_text("prompt2")
    assert client.captured["messages"] == [
        {"role": "user", "content": "prompt2"}]


def test_complete_text_blank_prompt_short_circuits() -> None:
    """空 prompt 直接返回空串，不发请求（省一次无效调用）。"""
    client = _FakeClient()
    p = OpenAIProvider(_cfg(), client=client)
    assert p.complete_text("   ") == ""
    assert client.captured is None, "空 prompt 不得发起请求"


def test_complete_text_requires_api_key() -> None:
    """缺 Key → ProviderError（与 translate_batch 一致，ADR-0004）。"""
    p = OpenAIProvider(_cfg(api_key=""), client=_FakeClient())
    with pytest.raises(ProviderError):
        p.complete_text("x")


def test_complete_text_wraps_errors() -> None:
    """网络/超时异常统一转 ProviderError（调用方负责静默降级）。"""
    p = OpenAIProvider(_cfg(), client=_BoomClient())
    with pytest.raises(ProviderError):
        p.complete_text("x")


def test_complete_text_falls_back_to_fallback_model() -> None:
    """主模型未配置时退到 fallback_model（信号 C 不要求单独配模型）。"""
    client = _FakeClient()
    p = OpenAIProvider(_cfg(model="", fallback_model="fb-model"), client=client)
    p.complete_text("x")
    assert client.captured["model"] == "fb-model"


# --------------------------------------------------------------------------- #
# config：term_extract_use_ai
# --------------------------------------------------------------------------- #
def test_config_term_extract_use_ai_default_true() -> None:
    """新字段默认 True（AI 辅助开启，但缺 Key 时自动降级）。"""
    assert Config().term_extract_use_ai is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ("", True),          # 空值无法识别 → 回落默认值 True
    ("garbage", True),   # 无法识别 → 回落默认值
])
def test_config_coerces_term_extract_use_ai_strings(raw: str, expected: bool) -> None:
    """ini 里的字符串按 configparser 布尔口径收敛。"""
    cfg = Config(term_extract_use_ai=raw)
    cfg._coerce_types()
    assert cfg.term_extract_use_ai is expected


def test_config_roundtrips_term_extract_use_ai(tmp_path) -> None:
    """存盘再读回：False 不丢失（§4 落点要求 ini + coerce 都要有）。"""
    cfg = Config(term_extract_use_ai=False)
    cfg.save(tmp_path)
    assert Config.load(tmp_path).term_extract_use_ai is False

    cfg2 = Config(term_extract_use_ai=True)
    cfg2.save(tmp_path)
    assert Config.load(tmp_path).term_extract_use_ai is True


def test_config_default_ini_has_terms_section(tmp_path) -> None:
    """首次生成的默认 config.ini 应含 [Terms] 段（用户可手改）。"""
    Config.load(tmp_path)
    text = (tmp_path / "config.ini").read_text(encoding="utf-8")
    assert "[Terms]" in text
    assert "term_extract_use_ai" in text


def test_config_other_fields_unaffected() -> None:
    """新字段不得影响既有字段的 coercion（回归护栏）。"""
    cfg = Config(max_tokens="999", temperature="0.7", timeout="60")
    cfg._coerce_types()
    assert cfg.max_tokens == 999
    assert cfg.temperature == 0.7
    assert cfg.timeout == 60.0
    assert cfg.term_extract_use_ai is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
