"""请求构建、响应三层解析与 §5.4 申报护栏测试（设计文档 §4.1、§4.2、§5.4、ADR-0002）。

覆盖 §11 `test_protocol` 一行的全部响应样本类别：围栏 / 多余行 / 缺行 / 坏 JSON /
编号乱序 / 退化成 `n|text`，以及 §5.4 四道护栏各一条用例。Provider 的网络层
（超时/重试/429 退避/日志脱敏）也在此覆盖；mock 只存在于 tests/（任务约束）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from pytran.config import Config, TermProfile
from pytran.interfaces import (
    KeepDeclared,
    ProtocolError,
    Provider as ProviderProtocol,
    ProviderError,
)
from pytran.protocol import (
    DEFAULT_SYSTEM_PROMPT,
    KEEP_DECLARE_HINT,
    MAX_CONTEXT_LINES,
    ParseResult,
    build_messages,
    build_request_params,
    build_system_prompt,
    build_user_prompt,
    normalize_keep_flag,
    parse_response,
)
from pytran.providers import OpenAIProvider


# ---------------------------------------------------------------------------
# §4.1 请求构建
# ---------------------------------------------------------------------------

def test_system_prompt_has_core_rules():
    for fragment in ("编号|文本", "只输出 JSON 行", "{w}", "[player_name]", "中文标点"):
        assert fragment in DEFAULT_SYSTEM_PROMPT


def test_system_prompt_profile_append_override():
    profile = TermProfile(prompt_overrides={"system_append": "补充：使用简体中文口语体。"})
    prompt = build_system_prompt(profile)
    assert prompt.startswith(DEFAULT_SYSTEM_PROMPT)
    assert "补充：使用简体中文口语体。" in prompt


def test_system_prompt_without_override_is_default():
    assert build_system_prompt(TermProfile()) == DEFAULT_SYSTEM_PROMPT


def test_user_prompt_has_terms_table():
    profile = TermProfile(terms={"Ian": "伊恩", "Lena": "莉娜"})
    text = build_user_prompt([(1, "Ian smiled.")], profile)
    assert "术语表" in text
    assert "Ian=伊恩" in text
    assert "Lena=莉娜" in text


def test_user_prompt_numbered_lines_and_count():
    text = build_user_prompt([(7, "One."), (8, "Two.")], TermProfile())
    assert "7|One." in text
    assert "8|Two." in text
    assert "2 行" in text


def test_user_prompt_context_pairs():
    ctx = [(101, "I saw her.", "我看见了她。"), (102, "She left.", "她走了。")]
    text = build_user_prompt([(1, "And then?"), ], TermProfile(), context=ctx)
    assert "前文参考" in text
    assert "101|I saw her." in text
    assert "我看见了她。" in text


def test_user_prompt_context_capped_at_three():
    ctx = [(i, f"s{i}", f"t{i}") for i in range(1, 6)]
    text = build_user_prompt([(1, "x")], TermProfile(), context=ctx)
    assert len(ctx) > MAX_CONTEXT_LINES
    assert "1|s1" in text
    assert "5|s5" not in text
    assert "3|s3" in text


def test_user_prompt_without_context_has_no_context_header():
    text = build_user_prompt([(1, "x")], TermProfile())
    assert "前文参考" not in text


def test_keep_declare_hint_only_injected_when_allowed():
    # §5.4 注入时机：只在重试轮（allow_keep_declare=True）出现，首轮保持最简
    assert KEEP_DECLARE_HINT not in build_user_prompt([(1, "x")], TermProfile())
    assert KEEP_DECLARE_HINT in build_user_prompt([(1, "x")], TermProfile(), allow_keep_declare=True)


def test_messages_shape():
    msgs = build_messages([(1, "x")], TermProfile())
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert all(set(m) == {"role", "content"} for m in msgs)


def test_request_params_only_standard_openai_keys():
    cfg = Config(api_key="sk-test", model="m1")
    params = build_request_params(cfg, build_messages([(1, "x")], TermProfile()))
    allowed = {"model", "messages", "temperature", "top_p", "max_tokens", "timeout"}
    assert set(params) <= allowed
    assert params["model"] == "m1"
    assert params["temperature"] == 0.2
    assert params["top_p"] == 0.9
    assert params["timeout"] == 120.0


def test_request_params_fallback_model_switch():
    cfg = Config(api_key="k", model="m1", fallback_model="m2")
    assert build_request_params(cfg, [])["model"] == "m1"
    assert build_request_params(cfg, [], use_fallback_model=True)["model"] == "m2"


def test_request_params_fallback_missing_keeps_primary():
    cfg = Config(api_key="k", model="m1", fallback_model="")
    assert build_request_params(cfg, [], use_fallback_model=True)["model"] == "m1"


def test_request_body_never_contains_api_key():
    cfg = Config(api_key="sk-secret-key", model="m1")
    body = repr(build_request_params(cfg, build_messages([(1, "x")], TermProfile())))
    assert "sk-secret-key" not in body


# ---------------------------------------------------------------------------
# §4.2 三层解析：§11 各类响应样本
# ---------------------------------------------------------------------------

def test_parse_plain_json_lines():
    raw = '{"i": 1, "t": "你好"}\n{"i": 2, "t": "世界"}'
    out = parse_response(raw, [1, 2])
    assert out.results == {1: "你好", 2: "世界"}
    assert out.missing == []
    assert out.extra == []


def test_parse_fenced_block():
    raw = '```json\n{"i": 1, "t": "你好"}\n{"i": 2, "t": "世界"}\n```'
    assert parse_response(raw, [1, 2]).results == {1: "你好", 2: "世界"}


def test_parse_shuffled_ids():
    raw = '{"i": 3, "t": "三"}\n{"i": 1, "t": "一"}\n{"i": 2, "t": "二"}'
    assert parse_response(raw, [1, 2, 3]).results == {1: "一", 2: "二", 3: "三"}


def test_parse_extra_uid_discarded():
    raw = '{"i": 1, "t": "一"}\n{"i": 99, "t": "多余"}'
    out = parse_response(raw, [1])
    assert out.results == {1: "一"}
    assert out.extra == [99]


def test_parse_missing_uid_recorded():
    out = parse_response('{"i": 1, "t": "一"}', [1, 2])
    assert out.missing == [2]
    assert out.results == {1: "一"}


def test_parse_degenerated_numbered_text():
    raw = "1|你好\n2|世界"
    assert parse_response(raw, [1, 2]).results == {1: "你好", 2: "世界"}


def test_parse_degenerated_keeps_pipe_in_text():
    assert parse_response("2|a|b", [2]).results == {2: "a|b"}


def test_parse_nonstandard_escape_recovered_by_regex():
    # 模型输出非法转义 \q：json.loads 失败，第二层正则兜住
    raw = '{"i": 1, "t": "a\\qb"}'
    out = parse_response(raw, [1])
    assert out.results == {1: "a\\qb"}
    assert out.unparsed == []


def test_parse_broken_json_line_is_unparsed():
    # 单引号 JSON 三层都救不回来 → 计入 unparsed，对应 uid 记为缺失
    raw = "{'i': 1, 't': '你好'}"
    out = parse_response(raw, [1])
    assert out.results == {}
    assert out.unparsed == ["{'i': 1, 't': '你好'}"]
    assert out.missing == [1]


def test_parse_skips_prose_lines():
    raw = '好的，以下是翻译结果：\n{"i": 1, "t": "你好"}\n\n希望对你有帮助。'
    out = parse_response(raw, [1])
    assert out.results == {1: "你好"}
    assert len(out.unparsed) == 2


def test_parse_duplicate_uid_keeps_first():
    raw = '{"i": 1, "t": "一"}\n{"i": 1, "t": "重复"}'
    out = parse_response(raw, [1])
    assert out.results == {1: "一"}
    assert out.duplicates == [1]


def test_parse_empty_response_raises_protocol_error():
    with pytest.raises(ProtocolError):
        parse_response("", [1, 2])


def test_parse_all_garbage_raises_protocol_error():
    with pytest.raises(ProtocolError):
        parse_response("抱歉，我无法完成该请求。", [1, 2])


def test_parse_partial_success_does_not_raise():
    out = parse_response('{"i": 1, "t": "一"}', [1, 2])
    assert out.results == {1: "一"} and out.missing == [2]


def test_parse_result_defaults_are_empty():
    result = ParseResult(results={1: "x"})
    assert result.missing == [] and result.extra == [] and result.unparsed == []
    assert result.declared == [] and result.rejected == {} and result.downgraded == []
    assert result.alert is None


# ---------------------------------------------------------------------------
# §5.4 四道护栏
# ---------------------------------------------------------------------------

SOURCES = {1: "Ian", 2: "Hahhh...", 3: "Hello there."}


def test_guardrail_1_declare_is_not_release():
    # 护栏 1：合法申报 → KeepDeclared 哨兵（进人工裁定队列），不是译文、不是 PASS
    raw = '{"i": 1, "t": "Ian", "k": 1, "r": "人名"}'
    out = parse_response(raw, [1], allow_keep_declare=True, sources=SOURCES)
    assert isinstance(out.results[1], KeepDeclared)
    assert out.results[1].reason == "人名"
    assert out.declared == [1]


def test_guardrail_1_keep_declared_is_not_plain_string():
    raw = '{"i": 1, "t": "Ian", "k": true, "r": "专名"}'
    out = parse_response(raw, [1], allow_keep_declare=True, sources=SOURCES)
    assert not isinstance(out.results[1], str)


def test_guardrail_1_empty_t_with_declare_is_accepted():
    # t 为空也视为「未改动」的一种形式
    raw = '{"i": 1, "t": "", "k": 1}'
    out = parse_response(raw, [1], allow_keep_declare=True, sources=SOURCES)
    assert isinstance(out.results[1], KeepDeclared)


def test_guardrail_2_declare_but_text_changed_is_rejected():
    # 护栏 2：申报却改了文本 → 拒绝，按 UNTRANSLATED 重译（返回原文文本+拒绝原因）
    raw = '{"i": 3, "t": "你好啊", "k": 1}'
    out = parse_response(raw, [3], allow_keep_declare=True, sources=SOURCES)
    assert out.results[3] == "你好啊"
    assert out.rejected == {3: "declared_but_changed"}
    assert not isinstance(out.results[3], KeepDeclared)


def test_guardrail_3_unknown_flag_variant_is_plain_text():
    # 护栏 3：未知变体按普通文本处理，绝不误判为申报
    raw = '{"i": 3, "t": "你好", "k": "maybe"}'
    out = parse_response(raw, [3], allow_keep_declare=True, sources=SOURCES)
    assert out.results[3] == "你好"
    assert out.declared == []


def test_guardrail_4_batch_declare_rate_downgrades_all():
    # 护栏 4：2/3 申报 > 15% → 全部申报降级为普通文本并告警
    raw = (
        '{"i": 1, "t": "Ian", "k": 1, "r": "人名"}\n'
        '{"i": 2, "t": "Hahhh...", "k": 1, "r": "拟声"}\n'
        '{"i": 3, "t": "你好", "k": false}'
    )
    out = parse_response(raw, [1, 2, 3], allow_keep_declare=True, sources=SOURCES)
    assert out.results[1] == "Ian"
    assert out.results[2] == "Hahhh..."
    assert sorted(out.downgraded) == [1, 2]
    assert out.alert and "申报率" in out.alert


def test_guardrail_4_rate_limit_is_configurable():
    raw = '{"i": 1, "t": "Ian", "k": 1}\n{"i": 3, "t": "你好", "k": false}'
    out = parse_response(raw, [1, 3], allow_keep_declare=True, sources=SOURCES,
                         keep_declare_rate_limit=0.9)
    assert isinstance(out.results[1], KeepDeclared)
    assert out.downgraded == []


def test_guardrail_4_low_rate_keeps_declaration():
    raw = '{"i": 1, "t": "Ian", "k": 1}\n{"i": 3, "t": "你好", "k": false}'
    out = parse_response(raw, [1, 2, 3], allow_keep_declare=True, sources=SOURCES)
    assert isinstance(out.results.get(1), KeepDeclared)


def test_keep_declare_ignored_when_not_injected():
    # 首轮（allow_keep_declare=False）即使模型自作主张输出 k，也不得生效
    raw = '{"i": 1, "t": "Ian", "k": 1, "r": "人名"}'
    out = parse_response(raw, [1], allow_keep_declare=False, sources=SOURCES)
    assert out.results[1] == "Ian"
    assert out.declared == []


def test_keep_declare_without_source_is_plain_text():
    # 无原文可比对（不得放行）→ 按普通文本
    raw = '{"i": 1, "t": "Ian", "k": 1}'
    out = parse_response(raw, [1], allow_keep_declare=True)
    assert out.results[1] == "Ian"
    assert out.declared == []


def test_normalize_keep_flag_variants():
    assert normalize_keep_flag(1) is True
    assert normalize_keep_flag("1") is True
    assert normalize_keep_flag(True) is True
    assert normalize_keep_flag("true") is True
    assert normalize_keep_flag("KEEP") is True
    assert normalize_keep_flag("yes") is True


def test_normalize_keep_flag_negative_variants():
    assert normalize_keep_flag(0) is False
    assert normalize_keep_flag("false") is False
    assert normalize_keep_flag("no") is False
    assert normalize_keep_flag(None) is False


def test_normalize_keep_flag_unknown_is_none():
    assert normalize_keep_flag("maybe") is None
    assert normalize_keep_flag(2) is None


# ---------------------------------------------------------------------------
# Provider：网络层（§4.1/§4.3、§12 日志脱敏、ADR-0004）
# ---------------------------------------------------------------------------

class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("unexpected extra call")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))]
        )


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class RateLimitError(Exception):
    status_code = 429


def _config(**kw) -> Config:
    base = {"api_key": "sk-test-key", "model": "m1"}
    base.update(kw)
    return Config(**base)


def _provider(responses, **cfg_kw) -> tuple[OpenAIProvider, FakeClient, list[float]]:
    client = FakeClient(responses)
    sleeps: list[float] = []
    cfg = _config(**cfg_kw)
    provider = OpenAIProvider(cfg, client=client, sleep=sleeps.append)
    return provider, client, sleeps


def test_provider_translates_and_parses():
    provider, client, _ = _provider(['{"i": 1, "t": "你好"}'])
    out = provider.translate_batch([(1, "Hello")], TermProfile())
    assert out == {1: "你好"}
    assert client.chat.completions.calls[0]["model"] == "m1"
    assert client.chat.completions.calls[0]["timeout"] == 120.0


def test_provider_passes_context_and_terms():
    provider, client, _ = _provider(['{"i": 1, "t": "你好"}'])
    profile = TermProfile(terms={"Ian": "伊恩"})
    provider.translate_batch([(1, "Ian")], profile, context=[(0, "a", "甲")])
    user = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "Ian=伊恩" in user
    assert "0|a" in user


def test_provider_keep_declare_threading():
    provider, client, _ = _provider(['{"i": 1, "t": "Ian", "k": 1}'])
    out = provider.translate_batch([(1, "Ian")], TermProfile(), allow_keep_declare=True)
    assert isinstance(out[1], KeepDeclared)
    assert KEEP_DECLARE_HINT in client.chat.completions.calls[0]["messages"][1]["content"]


def test_provider_fallback_model():
    provider, client, _ = _provider(['{"i": 1, "t": "你好"}'], fallback_model="m2")
    provider.translate_batch([(1, "Hello")], TermProfile(), use_fallback_model=True)
    assert client.chat.completions.calls[0]["model"] == "m2"


def test_provider_empty_batch_makes_no_call():
    provider, client, _ = _provider([])
    assert provider.translate_batch([], TermProfile()) == {}
    assert client.chat.completions.calls == []


def test_provider_retries_on_429_with_backoff():
    provider, _, sleeps = _provider([RateLimitError("slow down"), '{"i": 1, "t": "你好"}'])
    out = provider.translate_batch([(1, "Hello")], TermProfile())
    assert out == {1: "你好"}
    assert sleeps == [2.0, 4.0]


def test_provider_gives_up_after_attempts():
    provider, _, sleeps = _provider([RateLimitError("x"), RateLimitError("x"),
                                     RateLimitError("x"), RateLimitError("x")])
    with pytest.raises(ProviderError):
        provider.translate_batch([(1, "Hello")], TermProfile())
    assert len(sleeps) == 2  # 3 次尝试 → 2 次退避


def test_provider_does_not_retry_on_protocol_error():
    provider, client, _ = _provider(["抱歉，无法完成。"])
    with pytest.raises(ProtocolError):
        provider.translate_batch([(1, "Hello")], TermProfile())
    assert len(client.chat.completions.calls) == 1


def test_provider_satisfies_runtime_protocol():
    assert isinstance(OpenAIProvider(_config()), ProviderProtocol)


def test_provider_requires_api_key():
    provider = OpenAIProvider(Config(model="m1", api_key=""), client=FakeClient([]))
    with pytest.raises(Exception) as exc:
        provider.translate_batch([(1, "Hello")], TermProfile())
    assert "key" in str(exc.value).lower()


def test_provider_never_logs_key_or_headers(caplog):
    provider, _, _ = _provider(['{"i": 1, "t": "你好"}'])
    with caplog.at_level(logging.DEBUG, logger="pytran.providers"):
        provider.translate_batch([(1, "Hello")], TermProfile())
    text = caplog.text
    assert "sk-test-key" not in text
    assert "Authorization" not in text
    assert "headers" not in text.lower()


def test_provider_logs_truncated_response_preview(caplog):
    long_text = '{"i": 1, "t": "' + "啊" * 500 + '"}'
    provider, _, _ = _provider([long_text])
    with caplog.at_level(logging.DEBUG, logger="pytran.providers"):
        provider.translate_batch([(1, "Hello")], TermProfile())
    assert caplog.text  # 有日志
    assert "啊" * 500 not in caplog.text  # 只记前 N 字符（§12）
