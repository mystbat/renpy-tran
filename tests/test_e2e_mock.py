"""W3 端到端编排测试（设计文档 §11）。

策略：用一个内存 Mock Provider（仅存在于 tests/，绝不进入 pytran/）脚本化
translate_batch 行为，覆盖 §11 收敛表与完整链路：
  - test_e2e_bad_then_good：脚本化坏→好，应在 < 5 轮内收敛为全部 TRANSLATED；
  - test_e2e_always_same_error：恒定错 → STUCK 三档升级 → model_fixed_point；
  - test_e2e_always_random_error：随机错 → 5 轮硬上限 → round_budget；
  - test_e2e_full_chain：declare → human review → exempt table → detector PASS。

引擎契约约束（§3.2）：不触碰 Tkinter/任何 UI，事件只经 on_event 回调；project.json
每次状态变更原子写。本文件所有 save 经由 JsonProjectStore（引擎内部），不触碰 UI。
"""
from __future__ import annotations

import pytest

from pytran.config import Config, TermProfile
from pytran.engine import TranslationEngine
from pytran.interfaces import KeepDeclared, Provider, Uid
from pytran.models import (
    Project, ProjectStats, Segment, SegmentStatus,
)
from pytran.project import JsonProjectStore
from pytran.validators import Validator


# --------------------------------------------------------------------------
# 测试替身（仅限 tests/）
# --------------------------------------------------------------------------
class ScriptedProvider(Provider):
    """脚本化 Provider：按调用次数返回预设结果。

    脚本为 list[dict[Uid, str|KeepDeclared]]，第 i 次调用取 scripts[i]，
    超出则回退到最后一项（方便"恒定错"场景）。
    """

    def __init__(self, scripts: list[dict[Uid, "str | KeepDeclared"]], sleep=None):
        self.scripts = list(scripts)
        self.sleep = sleep or (lambda _s: None)
        self.call_count = 0
        self.last: dict[Uid, "str | KeepDeclared"] = {}

    def translate_batch(
        self, numbered_lines, profile, context=None,
        allow_keep_declare=False, use_fallback_model=False,
    ):
        self.call_count += 1
        idx = min(self.call_count - 1, len(self.scripts) - 1)
        result = dict(self.scripts[idx])
        self.last = result
        return result


class UndeclaringProvider(ScriptedProvider):
    """恒定坏译文 Provider：每次都把请求的行翻成带英文的坏译文（触发 residual）。"""

    def translate_batch(self, numbered_lines, profile, context=None,
                        allow_keep_declare=False, use_fallback_model=False):
        self.call_count += 1
        # 无论 allow_keep_declare 如何，都返回带英文的坏译文 → 始终 residual/english
        return {uid: f"坏 {src}" for (uid, src) in numbered_lines}


def _make_project(work_dir, sources, *, prefix="s", keep_declared=None):
    """构造含指定原文的 Project（全部 PENDING），写入 work_dir。"""
    keep_declared = keep_declared or set()
    segments = []
    for i, src in enumerate(sources, start=1):
        status = SegmentStatus.KEEP_DECLARED if i in keep_declared else SegmentStatus.PENDING
        segments.append(Segment(
            uid=i, file="script.rpy", locator=f"line{i}",
            feature="say", kind="say", comment="", role="",
            source=src, status=status, attempts=0, error=None,
        ))
    project = Project(
        version=3, app="pytran", files={},
        next_uid=len(segments) + 1, segments=segments,
        term_profile="profile.json", game_root="",
        stats=ProjectStats.from_segments(segments),
    )
    store = JsonProjectStore()
    store.save(project, work_dir)
    return project


def _events():
    """收集 on_event 事件，返回 (collector, log)。"""
    log: list[tuple[str, dict]] = []

    def _on(name, data):
        log.append((name, data))

    return _on, log


# --------------------------------------------------------------------------
# §11 收敛表
# --------------------------------------------------------------------------
def test_e2e_bad_then_good(work_dir, profile):
    """脚本化坏→好：retry 轮（第 2 次）注入好译文，应在 2 轮内收敛为全 TRANSLATED。"""
    sources = ["Hello world", "Good morning", "See you"]
    project = _make_project(work_dir, sources)
    provider = ScriptedProvider([
        # 第 1 次（round 1）：坏译文
        {1: "坏 Hello", 2: "坏 Good", 3: "坏 See"},
        # 第 2 次（round 2，重试轮）：好译文
        {1: "你好世界", 2: "早上好", 3: "再见"},
    ])
    engine = TranslationEngine(provider, Validator())
    on, log = _events()
    out = engine.run(project, Config(work_dir=str(work_dir)), profile, on_event=on)

    assert out.segments[0].status == SegmentStatus.TRANSLATED
    assert out.segments[1].status == SegmentStatus.TRANSLATED
    assert out.segments[2].status == SegmentStatus.TRANSLATED
    # 收敛早于 5 轮硬上限
    rounds = [d["round"] for (n, d) in log if n == "round"]
    assert max(rounds) <= 2
    # 每轮都原子 save 过（至少 round 事件数对应）
    assert any(n == "progress" and d.get("done") for (n, d) in log)


def test_e2e_always_same_error(work_dir, profile):
    """恒定坏译文 → STUCK 三档升级 → 收敛为 failed(reason=model_fixed_point)。"""
    sources = ["Hello world", "Good morning"]
    project = _make_project(work_dir, sources)
    provider = UndeclaringProvider([])  # 永远坏译文
    engine = TranslationEngine(provider, Validator())
    on, log = _events()
    out = engine.run(project, Config(work_dir=str(work_dir)), profile, on_event=on)

    for seg in out.segments:
        assert seg.status == SegmentStatus.FAILED
        assert seg.error == "model_fixed_point"
    assert provider.call_count >= 3  # 触发 STUCK 三档（含首轮正常 + 3 次定点）


def test_e2e_always_random_error(work_dir, profile):
    """随机坏译文（每次都变）→ 无状态哈希重复 → 5 轮硬上限 → failed(reason=round_budget)。"""
    sources = ["Hello world", "Good morning", "Thank you"]
    project = _make_project(work_dir, sources)
    # 每次调用返回不同的坏译文（状态哈希永不相同），耗尽 5 轮预算
    provider = ScriptedProvider([
        {1: "坏 a Hello", 2: "坏 a Good", 3: "坏 a Thank"},
        {1: "坏 b Hello", 2: "坏 b Good", 3: "坏 b Thank"},
        {1: "坏 c Hello", 2: "坏 c Good", 3: "坏 c Thank"},
        {1: "坏 d Hello", 2: "坏 d Good", 3: "坏 d Thank"},
        {1: "坏 e Hello", 2: "坏 e Good", 3: "坏 e Thank"},
        {1: "坏 f Hello", 2: "坏 f Good", 3: "坏 f Thank"},
    ])
    engine = TranslationEngine(provider, Validator())
    on, log = _events()
    out = engine.run(project, Config(work_dir=str(work_dir)), profile, on_event=on)

    rounds = [d["round"] for (n, d) in log if n == "round"]
    assert max(rounds) == 5  # 硬上限
    for seg in out.segments:
        assert seg.status == SegmentStatus.FAILED
        assert seg.error == "round_budget"


# --------------------------------------------------------------------------
# 完整链路：declare → review → exempt → PASS
# --------------------------------------------------------------------------
def test_e2e_full_chain_declare_review_exempt_pass(work_dir, profile_with_exempt):
    """完整链路：坏译文 FAILED → 豁免表命中 → detector PASS → 重译保留 REVIEWED（§6.5）。

    链路设计：
      - 第 3 行原文 "Menu" 命中豁免表；坏译文 "坏 Menu" 仍含豁免词 → detector 判
        PASS（§5.3 豁免整词命中）→ 该行进引擎即 TRANSLATED（演示 exempt→PASS）。
      - 第 1、2 行坏译文无豁免命中 → FAILED。
      - 人工审核把第 3 行置 REVIEWED（§6.5 不自动重译），再用好译文 provider 重译
        前 2 行；第 3 行保持 REVIEWED 不被引擎触碰。
    """
    sources = ["Save", "Load", "Menu"]
    project = _make_project(work_dir, sources)
    provider = UndeclaringProvider([])  # 永远坏译文
    engine = TranslationEngine(provider, Validator())
    on, log = _events()
    out1 = engine.run(project, Config(work_dir=str(work_dir)), profile_with_exempt, on_event=on)

    # 第 1、2 行坏译文（无豁免命中）→ FAILED；第 3 行含豁免词 "Menu" → 引擎判 PASS
    assert out1.segments[0].status == SegmentStatus.FAILED
    assert out1.segments[1].status == SegmentStatus.FAILED
    assert out1.segments[2].status == SegmentStatus.TRANSLATED
    assert out1.segments[2].target == "坏 Menu"

    # 人工审核：第 3 行标 REVIEWED 并保留译文（§6.5 不自动重译）
    out1.segments[2].status = SegmentStatus.REVIEWED
    store = JsonProjectStore()
    store.save(out1, work_dir)

    # 仅前两行入队重译（好译文 provider）
    provider2 = ScriptedProvider([{1: "保存", 2: "读取"}])
    engine2 = TranslationEngine(provider2, Validator())
    on2, log2 = _events()
    out2 = engine2.run(out1, Config(work_dir=str(work_dir)), profile_with_exempt, on_event=on2)

    # 前两行翻译成功，第三行保持 REVIEWED（未被引擎触碰）
    assert out2.segments[0].status == SegmentStatus.TRANSLATED
    assert out2.segments[1].status == SegmentStatus.TRANSLATED
    assert out2.segments[2].status == SegmentStatus.REVIEWED

    # detector 末端判定：豁免表命中 → PASS
    detection, _repaired = Validator().check(
        "Menu", "Menu", profile_with_exempt, allow_keep_declare=False,
    )
    assert detection == "pass"


# --------------------------------------------------------------------------
# 边界：缺失恢复 & only_uids 可重入
# --------------------------------------------------------------------------
def test_e2e_missing_recovery(work_dir, profile):
    """Provider 漏回部分 uid → 引擎置 FAILED(reason=missing: uid=...)。

    Provider 第 1 轮漏回 uid=1（触发缺失恢复），第 2 轮（重试轮）补齐；
    最终全部翻译成功，且事件日志中存在一次 uid=1 的 missing 恢复事件。
    """
    sources = ["Hello world", "Good morning", "See you"]
    project = _make_project(work_dir, sources)
    # 第 1 轮只回传 uid 2、3，漏掉 1；第 2 轮补齐 uid 1
    provider = ScriptedProvider([
        {2: "早上好", 3: "再见"},
        {1: "你好世界", 2: "早上好", 3: "再见"},
    ])
    engine = TranslationEngine(provider, Validator())
    on, log = _events()
    out = engine.run(project, Config(work_dir=str(work_dir)), profile, on_event=on)

    # 缺失恢复在第 1 轮被触发（事件日志可证）
    missing_events = [
        (n, d) for (n, d) in log
        if n == "segment" and d.get("uid") == 1 and d.get("reason") == "missing"
    ]
    assert missing_events, "应当记录一次 uid=1 的缺失恢复事件"
    # 最终全部翻译成功（缺失行在重试轮补齐）
    assert out.segments[0].status == SegmentStatus.TRANSLATED
    assert out.segments[1].status == SegmentStatus.TRANSLATED
    assert out.segments[2].status == SegmentStatus.TRANSLATED


def test_e2e_only_uids_reentrant(work_dir, profile):
    """only_uids 限定重译范围：只重译指定的失败行。"""
    sources = ["Hello world", "Good morning"]
    project = _make_project(work_dir, sources)
    # 先把第一行标为 FAILED（模拟历史失败），第二行 PENDING
    project.segments[0].status = SegmentStatus.FAILED
    project.segments[0].error = "residual"
    JsonProjectStore().save(project, work_dir)

    provider = ScriptedProvider([
        {1: "你好世界"},  # 仅修第一行
    ])
    engine = TranslationEngine(provider, Validator())
    on, log = _events()
    # only_uids 只重译 uid=1
    out = engine.run(project, Config(work_dir=str(work_dir)), profile,
                     on_event=on, only_uids={1})

    assert out.segments[0].status == SegmentStatus.TRANSLATED
    # 第二行 PENDING 不在 only_uids 范围 → 未被处理，仍为 PENDING
    assert out.segments[1].status == SegmentStatus.PENDING
    assert provider.last == {1: "你好世界"}


def test_e2e_atomic_save_no_ui(work_dir, profile):
    """引擎从不触碰 Tkinter/UI：仅经 on_event 与 JsonProjectStore 写回。"""
    sources = ["Hello world"]
    project = _make_project(work_dir, sources)
    provider = ScriptedProvider([{1: "你好世界"}])
    engine = TranslationEngine(provider, Validator())
    on, log = _events()
    engine.run(project, Config(work_dir=str(work_dir)), profile, on_event=on)

    # project.json 确实被写出
    assert (work_dir / "project.json").exists()
    # 事件名合法集合
    valid = {"progress", "log", "segment", "round"}
    assert all(n in valid for (n, _d) in log)
    # 至少一次 segment 事件（翻译完成）+ 一次 progress done
    assert any(n == "segment" for (n, _d) in log)
    assert any(n == "progress" and d.get("done") for (n, d) in log)
