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
    KEEP_DECLARE_CUM_MIN_ROWS,
    KEEP_DECLARE_MIN_BATCH,
    KeepDeclareAccumulator,
    MAX_CONTEXT_LINES,
    ParseResult,
    build_messages,
    build_request_params,
    build_system_prompt,
    build_user_prompt,
    normalize_keep_flag,
    parse_response,
)
from pytran.providers import TIMEOUT_MAX, TIMEOUT_REF_LINES, OpenAIProvider


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
    # BUGFIX(ctx-uid-collision)：context 行不带数字编号（防与待翻译 uid 撞车）
    assert "101|I saw her." not in text
    assert '"I saw her."' in text
    assert "我看见了她。" in text


def test_user_prompt_context_no_numbered_collision():
    """BUGFIX(ctx-uid-collision)：context 区不得出现任何「数字|」形态的行。

    旧实现 context 用 `{uid}|{src}` 展示，_normalize_context 对二元组按序
    补号 0,1,2...，与重导后从 1 开始的待翻译 uid 大面积重叠——模型据此
    输出错位（用户实测 2026-10-24：写回后重导新文件，预览译文错位）。
    """
    import re
    # 经 provider._normalize_context 归一化后的二元组 context（位置补号 0,1）
    ctx = [(0, "Old one", "旧译1"), (1, "Old two", "旧译2")]
    text = build_user_prompt([(1, "New one"), (2, "New two")],
                             TermProfile(),
                             context=ctx)
    # 待翻译区保留编号
    assert "1|New one" in text
    assert "2|New two" in text
    # context 区不得有编号行
    ctx_block = text.split("前文参考")[1].split("待翻译")[0]
    assert not re.search(r"^\s*\d+\s*\|", ctx_block, re.M), ctx_block
    # context 原文仍以引号形式展示（供语气衔接）
    assert '"Old one"' in ctx_block


def test_user_prompt_context_capped_at_three():
    # DECISION(§4.1)：超出上限时保留**最新**的 3 条（与待翻行最近的上下文最有价值）
    ctx = [(i, f"s{i}", f"t{i}") for i in range(1, 6)]
    text = build_user_prompt([(1, "x")], TermProfile(), context=ctx)
    assert len(ctx) > MAX_CONTEXT_LINES
    assert '"s1"' not in text
    assert '"s2"' not in text
    assert '"s3"' in text
    assert '"s5"' in text


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
    # 坏 JSON 行（单引号）三层都救不回来 → 计入 unparsed，对应 uid 记为缺失；
    # 同批里能救回的行照常产出（部分成功不抛异常，避免整批白翻，DECISION(§4.3)）
    raw = '{"i": 1, "t": "一"}\n{\'i\': 2, \'t\': \'二\'}'
    out = parse_response(raw, [1, 2])
    assert out.results == {1: "一"}
    assert out.unparsed == ["{'i': 2, 't': '二'}"]
    assert out.missing == [2]


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


def test_guardrail_4_no_downgrade_below_min_batch():
    # 双闸·单批闸不触发：批内样本 < KEEP_DECLARE_MIN_BATCH（此处 3 行）时，
    # 即使申报率 2/3 = 67% >> 15%，也不降级——单行/小批的合法申报（人名/拟声）
    # 交给护栏 1/2 逐条进人工裁定。
    assert KEEP_DECLARE_MIN_BATCH == 5
    raw = (
        '{"i": 1, "t": "Ian", "k": 1, "r": "人名"}\n'
        '{"i": 2, "t": "Hahhh...", "k": 1, "r": "拟声"}\n'
        '{"i": 3, "t": "你好", "k": false}'
    )
    out = parse_response(raw, [1, 2, 3], allow_keep_declare=True, sources=SOURCES)
    assert out.downgraded == []
    assert out.alert is None
    assert isinstance(out.results.get(1), KeepDeclared)
    assert isinstance(out.results.get(2), KeepDeclared)


def test_guardrail_4_rate_limit_is_configurable():
    raw = '{"i": 1, "t": "Ian", "k": 1}\n{"i": 3, "t": "你好", "k": false}'
    out = parse_response(raw, [1, 3], allow_keep_declare=True, sources=SOURCES,
                         keep_declare_rate_limit=0.9)
    assert isinstance(out.results[1], KeepDeclared)
    assert out.downgraded == []


def test_guardrail_4_low_rate_keeps_declaration():
    # 批内样本 < 下限：申报率虽"高"（1/2 = 50%），但小批不降级，申报保留。
    raw = '{"i": 1, "t": "Ian", "k": 1}\n{"i": 3, "t": "你好", "k": false}'
    out = parse_response(raw, [1, 2, 3], allow_keep_declare=True, sources=SOURCES)
    assert isinstance(out.results.get(1), KeepDeclared)


def test_guardrail_4_downgrade_when_min_batch_and_over_rate():
    # 双闸·单批闸触发：批内样本 ≥ 5 且申报率 > 15% → 整批申报降级为普通译文并告警。
    # 5 行里申报 2 行 = 40% > 15%，超过 KEEP_DECLARE_MIN_BATCH 下限。
    raw = (
        '{"i": 1, "t": "Ian", "k": 1, "r": "人名"}\n'
        '{"i": 2, "t": "Hahhh...", "k": 1, "r": "拟声"}\n'
        '{"i": 3, "t": "你好", "k": false}\n'
        '{"i": 4, "t": "Bob", "k": 1, "r": "人名"}\n'
        '{"i": 5, "t": "再见", "k": false}'
    )
    out = parse_response(raw, [1, 2, 3, 4, 5], allow_keep_declare=True, sources=SOURCES)
    assert sorted(out.downgraded) == [1, 2, 4]
    assert out.alert and "申报率" in out.alert
    assert out.results[1] == "Ian"
    assert out.results[4] == "Bob"


def test_guardrail_4_accumulator_detects_systemic_abuse():
    # 双闸·累计闸：每批都 <5 行、单批闸全放过，但"每批都申报一大半"→
    # 累计样本达 KEEP_DECLARE_CUM_MIN_ROWS(20) 且累计率 >15% 时进入降级模式并告警。
    acc = KeepDeclareAccumulator()
    # 20 批，每批 1 行且全部申报（累计 20/20 = 100% > 15%）
    for _ in range(KEEP_DECLARE_CUM_MIN_ROWS):
        out = parse_response('{"i": 1, "t": "Ian", "k": 1, "r": "人名"}',
                             [1], allow_keep_declare=True, sources=SOURCES,
                             accumulator=acc)
        assert out.downgraded == []           # 单批闸（1 行）不降级
        assert isinstance(out.results.get(1), KeepDeclared)
    assert acc.degrade_mode is True
    # 首次越累计阈的一批 cumulative_alert 应非 None
    early = parse_response('{"i": 1, "t": "Ian", "k": 1, "r": "人名"}',
                           [1], allow_keep_declare=True, sources=SOURCES,
                           accumulator=acc)
    assert early.cumulative_alert is not None
    assert "降级模式" in early.cumulative_alert


def test_guardrail_4_accumulator_only_after_min_rows():
    # 累计闸在样本达下限前不触发：19 行累计且全申报（19/19=100%）仍 < 20 行 → 未降级。
    acc = KeepDeclareAccumulator()
    for _ in range(KEEP_DECLARE_CUM_MIN_ROWS - 1):
        parse_response('{"i": 1, "t": "Ian", "k": 1, "r": "人名"}',
                       [1], allow_keep_declare=True, sources=SOURCES,
                       accumulator=acc)
    assert acc.degrade_mode is False
    # 再补一行（恰达下限）全申报 → 越过阈，进入降级模式
    parse_response('{"i": 1, "t": "Ian", "k": 1, "r": "人名"}',
                   [1], allow_keep_declare=True, sources=SOURCES, accumulator=acc)
    assert acc.degrade_mode is True


def test_guardrail_4_accumulator_irreversible():
    # 进入降级模式后不可逆：即便后续批次零申报，degrade_mode 保持 True。
    acc = KeepDeclareAccumulator()
    for _ in range(KEEP_DECLARE_CUM_MIN_ROWS):
        parse_response('{"i": 1, "t": "Ian", "k": 1, "r": "人名"}',
                       [1], allow_keep_declare=True, sources=SOURCES, accumulator=acc)
    assert acc.degrade_mode is True
    # 后续 10 批零申报
    for _ in range(10):
        parse_response('{"i": 3, "t": "你好", "k": false}',
                       [3], allow_keep_declare=True, sources=SOURCES, accumulator=acc)
    assert acc.degrade_mode is True


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
    # BUGFIX(ctx-uid-collision)：context 行不带编号，只展示原文/译文对
    assert '"a"' in user and "甲" in user
    assert "0|a" not in user


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
    # 三次响应：两次 429 + 一次成功 → 退避 2.0s 后第 2 次、4.0s 后第 3 次
    provider, _, sleeps = _provider([RateLimitError("slow down"), RateLimitError("slow down"),
                                     '{"i": 1, "t": "你好"}'])
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


# ---------------------------------------------------------------------------
# BUGFIX(timeout-amplify)：超时不得被 SDK 层放大，且须随批量放大
#
# 用户实测（2026-10-24，清空工程重新导入后点翻译）：
#   进度条一直滑 → APITimeoutError → 「2s 后重试（1/2）」循环。
# 根因：openai SDK 默认 max_retries=2 叠在 _request_with_retry 之上，
# 一次逻辑尝试 = 3 × timeout ≈ 181.9s；且 60s 固定超时对 50 行满块本就不够。
# ---------------------------------------------------------------------------


def test_build_client_disables_sdk_level_retries(monkeypatch):
    """SDK 自带重试必须关掉，否则 3×timeout 放大（BUGFIX(timeout-amplify)）。"""
    import openai

    captured: dict = {}

    def spy_init(self, *args, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        captured["_args"] = args
        # 不真正构造：避免需要网络/校验，仅断言入参
        self.__dict__["_spy"] = True

    monkeypatch.setattr(openai.OpenAI, "__init__", spy_init)
    OpenAIProvider._build_client(Config(api_key="sk-test-key", model="m1"))

    assert captured.get("max_retries") == 0, \
        f"SDK 重试必须显式置 0，实际 {captured.get('max_retries')!r}"
    assert captured.get("timeout") == 120.0
    assert captured.get("api_key") == "sk-test-key"


def test_effective_timeout_scales_with_line_count():
    """满块请求按行数放大超时，且有硬上限（BUGFIX(timeout-amplify)）。"""
    provider = OpenAIProvider(_config(timeout=60.0), client=FakeClient([]))
    assert provider._effective_timeout(0) == 60.0
    assert provider._effective_timeout(1) == 60.0
    assert provider._effective_timeout(TIMEOUT_REF_LINES) == 60.0
    assert provider._effective_timeout(TIMEOUT_REF_LINES + 1) == 120.0  # 向上取整
    assert provider._effective_timeout(50) == 300.0                     # block_lines=50
    assert provider._effective_timeout(10_000) == TIMEOUT_MAX           # 硬上限


def test_translate_batch_timeout_covers_whole_block():
    """50 行满块的实际请求超时必须远超旧的固定 60s（用户 bug 的直接成因）。"""
    provider, client, _ = _provider(
        ['{"i": 1, "t": "你好"}'], timeout=60.0, block_lines=50,
    )
    lines = [(i, f"Hello {i}") for i in range(1, 51)]
    provider.translate_batch(lines, TermProfile())
    sent_timeout = client.chat.completions.calls[0]["timeout"]
    assert sent_timeout == 300.0, f"50 行应放大到 300s，实际 {sent_timeout}"


def test_single_logical_attempt_bounded_by_one_timeout():
    """一次逻辑尝试的总耗时上限 = timeout + 退避，不被 SDK 再乘 3 倍。

    回归口径：max_retry_rounds=1 时，translate_batch 只能发出 1 次请求。
    """
    provider, client, sleeps = _provider(
        [TimeoutError("timed out")], timeout=60.0, max_retry_rounds=1,
    )
    with pytest.raises(ProviderError):
        provider.translate_batch([(1, "Hello")], TermProfile())
    assert len(client.chat.completions.calls) == 1  # 无 SDK 层重复请求
    assert sleeps == []                             # 无退避（只有 1 次尝试）
