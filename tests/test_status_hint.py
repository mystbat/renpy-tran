"""FEATURE(status-hint)：429 等「服务器未正确回复」必须在底部状态栏可见。

用户实测反馈：429（或超时/协议不可解析）时服务器没有正常回复，界面只在
进度条上走一格，随后整块行静默变红——底部状态栏一个字都没有，看不出发生了
什么，也无法判断是限流、超时还是模型乱答。

根因：``TranslationEngine._translate_round`` 用 ``logger.exception`` 吞掉块级
异常后把 ``result`` 置空，行级只留 ``reason="missing"``；而本工程所有模块只用
``logging.getLogger(__name__)``、没有配置任何 handler，所以
``logger.warning/exception`` 永远到不了 UI。

本用例锁定修复后的契约：
- 块级失败 → 引擎发 ``log`` 事件，``level="warn"``，消息里含人能看懂的原因；
- 429 / 超时 / 协议错误 / 鉴权 / 5xx 分别给出不同措辞；
- 同一原因多块失败不刷屏（1 条详情 + 1 条汇总）；
- 行级 ``reason`` 仍为 ``"missing"``（不扰动 ``_state_key`` 的 STUCK 判定）；
- 不引入新的 UI 事件名（``test_e2e_atomic_save_no_ui`` 依赖合法集合）。
"""
from __future__ import annotations

import pytest

from pytran.config import Config
from pytran.engine import TranslationEngine, _failure_hint
from pytran.interfaces import Detection, ProtocolError, ProviderError
from pytran.models import Project, Segment
from pytran.project import JsonProjectStore
from pytran.terms import TermProfile


class PassValidator:
    def check(self, source, target, profile, allow_keep_declare=False):
        return Detection.PASS.value, None


class FailingProvider:
    """每批都抛指定异常，模拟「服务器端没有正确回复」。"""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def translate_batch(self, numbered_lines, profile, context=(),
                        allow_keep_declare=False, use_fallback_model=False):
        self.calls += 1
        raise self.exc

    def review_text(self, *a, **k):
        return ""


class RateLimitError(Exception):
    """模拟 SDK 的 RateLimitError（带 status_code 属性）。"""

    status_code = 429


def _project(n: int = 10) -> Project:
    return Project(segments=[
        Segment(uid=i, file="a.rpy", locator=f"a.rpy:{i}", feature="say",
                kind="dialogue", comment="", role="", source=f"line {i}")
        for i in range(n)])


def _run(tmp_path, exc, *, block_lines=100, round_budget_cap=1):
    prov = FailingProvider(exc)
    eng = TranslationEngine(prov, PassValidator(), store=JsonProjectStore())
    cfg = Config(
        api_key="k", model="primary", fallback_model="",
        work_dir=str(tmp_path), max_workers=1, block_lines=block_lines,
        round_budget_cap=round_budget_cap, max_retry_rounds=1,
    )
    log: list[tuple[str, dict]] = []
    project = _project()
    eng.run(project, cfg, TermProfile(),
            on_event=lambda n, d: log.append((n, dict(d or {}))))
    return prov, log, project


def _messages(log) -> list[str]:
    return [d.get("message", "") for (n, d) in log if n == "log"]


def _warn_messages(log) -> list[str]:
    return [d.get("message", "") for (n, d) in log
            if n == "log" and d.get("level") == "warn"]


# --------------------------------------------------------------------------- #
# 核心回归：用户报的那个 bug
# --------------------------------------------------------------------------- #
def test_429_failure_surfaces_in_log_event(tmp_path):
    """429 时引擎必须发出一条 warn 级 log 事件（修复前：一条都没有）。"""
    _, log, _proj = _run(tmp_path, ProviderError("重试 1 次仍失败：RateLimitError"))

    assert _warn_messages(log), "429 块级失败必须产生 warn 级 log 事件"
    joined = " ".join(_messages(log))
    assert "429" in joined or "限流" in joined, f"提示里必须点明限流，实际：{joined!r}"


def test_timeout_failure_surfaces_with_timeout_wording(tmp_path):
    """超时 → 提示要说是超时，而不是笼统的「失败」。"""
    _, log, _proj = _run(tmp_path, ProviderError("重试 2 次仍失败：APITimeoutError"))
    joined = " ".join(_warn_messages(log))
    assert "超时" in joined, f"超时应被点明，实际：{joined!r}"


def test_protocol_error_surfaces_with_parse_wording(tmp_path):
    """协议不可解析 → 提示要说是「回复无法解析」，别让用户以为网络坏了。"""
    _, log, _proj = _run(tmp_path, ProtocolError("响应为空：无法解析任何编号行"))
    joined = " ".join(_warn_messages(log))
    assert "解析" in joined, f"协议错误应点明无法解析，实际：{joined!r}"


def test_auth_error_surfaces_as_key_problem(tmp_path):
    """401/403 → 提示指向 API Key，用户才知道该去改设置。"""
    _, log, _proj = _run(tmp_path, ProviderError("调用失败（不可重试）：AuthenticationError"))
    joined = " ".join(_warn_messages(log))
    assert "Key" in joined or "权限" in joined, f"鉴权错误应指向 Key，实际：{joined!r}"


def test_message_includes_uid_range_and_row_count(tmp_path):
    """提示要能让用户定位：带行号区间与行数。"""
    _, log, _proj = _run(tmp_path, ProviderError("重试 1 次仍失败：RateLimitError"))
    joined = " ".join(_messages(log))
    assert "0..9" in joined, f"提示应含行号区间，实际：{joined!r}"
    assert "10 行" in joined, f"提示应含失败行数，实际：{joined!r}"


# --------------------------------------------------------------------------- #
# 不刷屏：同一原因多块失败只详述一次
# --------------------------------------------------------------------------- #
def test_same_reason_many_chunks_is_deduplicated(tmp_path):
    """10 行按 block_lines=2 切 → 5 块全 429，只写 1 条详情 + 1 条汇总。"""
    _, log, _proj = _run(tmp_path,
                  ProviderError("重试 1 次仍失败：RateLimitError"),
                  block_lines=2)
    chunk_msgs = [m for m in _warn_messages(log) if "块失败" in m]
    assert len(chunk_msgs) == 1, f"同因多块应去重为 1 条，实际 {len(chunk_msgs)} 条"
    summary = [m for m in _warn_messages(log) if "未拿到有效回复" in m]
    assert len(summary) == 1, f"应有一条轮末汇总，实际 {len(summary)} 条"
    assert "5 块" in summary[0], f"汇总应给出块数，实际：{summary[0]!r}"


def test_single_chunk_has_no_redundant_summary(tmp_path):
    """只 1 块失败时不重复汇总（详情已说清楚）。"""
    _, log, _proj = _run(tmp_path, ProviderError("重试 1 次仍失败：RateLimitError"))
    summary = [m for m in _warn_messages(log) if "未拿到有效回复" in m]
    assert not summary, f"单块失败不该再补汇总，实际：{summary!r}"


# --------------------------------------------------------------------------- #
# 契约保护：不扰动行级 reason / 事件名集合
# --------------------------------------------------------------------------- #
def test_row_level_reason_stays_missing(tmp_path):
    """块级失败落到行上时 reason 必须仍是 "missing"。

    ``_state_key`` 只哈希 ``uid:target:error``；若把「429/超时」写进行级
    ``reason`` 或 ``seg.error``，STUCK 判定与回退模型切换
    （FEATURE(fallback-after-failures)）的行为会被这次「只加提示」的改动带偏。
    （终止阶段的 ``round_budget`` 是既有 ``_force_fail`` 行为，不属本测试范围。）
    """
    _, log, project = _run(tmp_path, ProviderError("重试 1 次仍失败：RateLimitError"))
    reasons = {d.get("reason") for (n, d) in log
               if n == "segment" and d.get("status") == "failed"}
    assert "missing" in reasons, f"块级失败应记为 missing，实际 {reasons!r}"
    leaked = {r for r in reasons if r and ("限流" in r or "429" in r)}
    assert not leaked, f"提示语不得写入行级 reason，实际泄漏 {leaked!r}"
    # 落盘的 error 也不得带提示语（同上：_state_key 哈希 error）
    assert all("限流" not in (seg.error or "") for seg in project.segments)


def test_no_new_event_names_introduced(tmp_path):
    """只加 log 事件，不引入新事件名（e2e 合法集合测试依赖此约定）。"""
    _, log, _proj = _run(tmp_path, ProviderError("重试 1 次仍失败：RateLimitError"),
                         block_lines=2)
    valid = {"progress", "log", "segment", "round", "chunk_done"}
    assert {n for (n, _d) in log} <= valid


def test_round_budget_exhaustion_is_also_reported(tmp_path):
    """轮次预算用尽也要留痕，否则用户不知「为何停了」。"""
    _, log, _proj = _run(tmp_path, ProviderError("重试 1 次仍失败：RateLimitError"))
    joined = " ".join(_warn_messages(log))
    assert "轮次预算" in joined, f"预算用尽应提示，实际：{joined!r}"


# --------------------------------------------------------------------------- #
# 归类函数单测
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc,expect", [
    (ProviderError("重试 1 次仍失败：RateLimitError"), "限流"),
    (RateLimitError("TPM limit reached"), "限流"),
    (ProviderError("重试 2 次仍失败：APITimeoutError"), "超时"),
    (ProtocolError("响应为空：无法解析任何编号行"), "解析"),
    (ProviderError("调用失败（不可重试）：AuthenticationError"), "Key"),
    (ProviderError("重试 2 次仍失败：InternalServerError"), "5xx"),
    (ProviderError("重试 1 次仍失败：APIConnectionError"), "网络"),
])
def test_failure_hint_classification(exc, expect):
    hint = _failure_hint(exc)
    assert expect in hint, f"{type(exc).__name__} 应归类为 {expect}，实际：{hint!r}"


def test_failure_hint_falls_back_to_exception_name():
    """无法归类的异常也要说清类型，别吞成空白。"""
    hint = _failure_hint(ValueError("boom"))
    assert "ValueError" in hint and "boom" in hint


def test_failure_hint_truncates_huge_message():
    """超长原始消息要截断，否则会撑爆状态栏。"""
    hint = _failure_hint(ValueError("x" * 500))
    assert len(hint) < 250, f"提示过长：{len(hint)}"
