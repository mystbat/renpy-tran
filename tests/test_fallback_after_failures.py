"""FEATURE(fallback-after-failures)：主模型连续失败 N 次后切回退模型。

覆盖：
- 默认 0 = 不回退（行为与旧版一致，STUCK 三档照旧）；
- 配置 N 后，第 N 个失败轮次切到回退模型；
- 切过去后回退模型拿到足够轮次（不被 round_budget_cap 提前截断）；
- 配了次数但没配 fallback_model → 不崩、不空转、给出告警；
- 0 在 UI 层不被钳成 1（0 是合法语义）。
"""
from __future__ import annotations

import pytest

from pytran.config import Config
from pytran.engine import FALLBACK_MIN_ROUNDS, TranslationEngine
from pytran.interfaces import Detection
from pytran.models import Project, Segment
from pytran.project import JsonProjectStore
from pytran.terms import TermProfile


class RecordingFailProvider:
    """每批都抛异常，记录每次调用的 use_fallback_model。"""

    def __init__(self):
        self.calls: list[bool] = []

    def translate_batch(self, numbered_lines, profile, context=(),
                        allow_keep_declare=False, use_fallback_model=False):
        self.calls.append(bool(use_fallback_model))
        raise RuntimeError("RateLimitError: TPM limit reached")

    def review_text(self, *a, **k):
        return ""


class PassValidator:
    def check(self, source, target, profile, allow_keep_declare=False):
        return Detection.PASS.value, None


def _project(n=4):
    return Project(segments=[
        Segment(uid=i, file="f.rpy", locator=f"f.rpy:{i}", feature="say",
                kind="say", comment="", role="", source=f"line {i}")
        for i in range(n)])


def _run(tmp_path, *, fallback_model="backup", fallback_after=0,
         round_budget_cap=5, max_retry_rounds=2):
    prov = RecordingFailProvider()
    eng = TranslationEngine(prov, PassValidator(), store=JsonProjectStore())
    cfg = Config(
        api_key="k", model="primary", fallback_model=fallback_model,
        work_dir=str(tmp_path), max_workers=1, block_lines=100,
        round_budget_cap=round_budget_cap, max_retry_rounds=max_retry_rounds,
        fallback_after_failures=fallback_after,
    )
    proj = _project()
    eng.run(proj, cfg, TermProfile())
    return prov, proj


# --------------------------------------------------------------------------- #
# 默认关闭 = 行为不变
# --------------------------------------------------------------------------- #
def test_default_zero_never_uses_fallback(tmp_path):
    """0 = 彻底禁用回退，连 STUCK 第 2 档的「切备用模型」也不做。

    用户明确要求：填 0 时不回退。故本用例断言**全程零次**回退调用。
    """
    prov, _ = _run(tmp_path, fallback_after=0)
    assert prov.calls, "应该至少跑了一轮"
    assert not any(prov.calls), "fallback_after_failures=0 时必须完全不回退"


def test_default_config_value_is_zero():
    """默认值必须是 0，保证旧 ini 加载后行为不变。"""
    assert Config().fallback_after_failures == 0


# --------------------------------------------------------------------------- #
# 启用后按失败次数切换
# --------------------------------------------------------------------------- #
def test_switches_to_fallback_after_configured_failures(tmp_path):
    """配 2 → 第 2 个失败轮次切过去，后续调用都用回退模型。"""
    prov, _ = _run(tmp_path, fallback_after=2)
    assert True in prov.calls, "配了次数却没切回退模型"
    first_fb = prov.calls.index(True)
    assert first_fb >= 1, "不应在第 1 轮就切（还没失败过）"
    # 一旦切换，之后应一直用回退模型
    assert all(prov.calls[first_fb:]), "切换后不应再退回主模型"


def test_switch_happens_earlier_than_stuck_stage_two(tmp_path):
    """按次数切换比 STUCK 三档更快：配 1 时第 2 轮就该切。"""
    prov, _ = _run(tmp_path, fallback_after=1)
    assert prov.calls[0] is False, "第 1 轮必须先用主模型"
    assert prov.calls[1] is True, "第 2 轮应已切到回退模型"


def test_fallback_gets_minimum_rounds(tmp_path):
    """回退模型不能被 round_budget_cap 提前截断——这是本 feature 的核心。"""
    prov, _ = _run(tmp_path, fallback_after=1, round_budget_cap=5)
    fb_rounds = sum(1 for c in prov.calls if c)
    assert fb_rounds >= FALLBACK_MIN_ROUNDS, (
        f"回退模型只拿到 {fb_rounds} 轮，应至少 {FALLBACK_MIN_ROUNDS} 轮")


def test_fallback_rounds_exceed_old_behaviour(tmp_path):
    """回归保护：旧版回退只拿到 1 轮，现在必须更多。"""
    prov, _ = _run(tmp_path, fallback_after=1, round_budget_cap=5)
    assert sum(1 for c in prov.calls if c) > 1


# --------------------------------------------------------------------------- #
# 边界：配了次数但没配模型
# --------------------------------------------------------------------------- #
def test_no_fallback_model_configured_does_not_crash(tmp_path):
    """配了次数但 fallback_model 为空 → 不崩，且从不发 use_fallback=True。"""
    prov, proj = _run(tmp_path, fallback_model="", fallback_after=1)
    assert not any(prov.calls)
    assert all(s.status.value == "failed" for s in proj.segments)


def test_all_rows_end_failed_when_provider_always_fails(tmp_path):
    """provider 恒失败 → 最终全部 failed（不静默成功）。"""
    _, proj = _run(tmp_path, fallback_after=1)
    assert all(s.status.value == "failed" for s in proj.segments)


# --------------------------------------------------------------------------- #
# UI 层收敛：0 是合法语义，不能被钳成 1
# --------------------------------------------------------------------------- #
def test_ui_coerce_keeps_zero():
    from pytran.ui import PytranApp
    assert PytranApp._coerce_setting("fallback_after_failures", "0") == 0


def test_ui_coerce_parses_positive():
    from pytran.ui import PytranApp
    assert PytranApp._coerce_setting("fallback_after_failures", "3") == 3


def test_ui_coerce_blank_keeps_existing():
    """留空 → None（保留既有值），与其它数值字段同口径。"""
    from pytran.ui import PytranApp
    assert PytranApp._coerce_setting("fallback_after_failures", "") is None


def test_ui_coerce_negative_clamped_to_zero():
    from pytran.ui import PytranApp
    assert PytranApp._coerce_setting("fallback_after_failures", "-5") == 0


def test_ui_coerce_garbage_keeps_existing():
    from pytran.ui import PytranApp
    assert PytranApp._coerce_setting("fallback_after_failures", "abc") is None


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def test_roundtrips_through_ini(tmp_path):
    cfg = Config(api_key="k", model="m", fallback_after_failures=3,
                 work_dir=str(tmp_path))
    cfg.save(tmp_path)
    assert Config.load(tmp_path).fallback_after_failures == 3


def test_missing_key_defaults_to_zero(tmp_path):
    """旧 config.ini（无该键）→ 0，行为不变。"""
    (tmp_path / "config.ini").write_text(
        "[Model]\nmodel = m1\n", encoding="utf-8")
    assert Config.load(tmp_path).fallback_after_failures == 0
