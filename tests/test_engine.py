"""TranslationEngine._build_queue（W5g §2 根因修复）单测。

W5g §2 的核心约束：重译队列只收 PENDING / FAILED，KEEP_DECLARED 与
KEEP_ORIGINAL 均不进队列——否则「已被接受/已声明保留/已人工保留原文」的段
会在「全量翻译 / 失败重试 / 试译」三处被自动重发覆盖。

_build_queue 是静态方法，无需 Provider，纯逻辑构造即可断言。
"""
from __future__ import annotations

import pytest

from pytran.engine import TranslationEngine
from pytran.models import Project, Segment, SegmentStatus


def _seg(uid, status):
    return Segment(
        uid=uid, file="a.rpy", locator=f"loc{uid}", feature=f"f{uid}",
        kind="dialogue", comment="", role="", source=f"src{uid}",
        target=None, status=status,
    )


def _project() -> Project:
    return Project(segments=[
        _seg(1, SegmentStatus.PENDING),
        _seg(2, SegmentStatus.FAILED),
        _seg(3, SegmentStatus.TRANSLATED),
        _seg(4, SegmentStatus.REVIEWED),
        _seg(5, SegmentStatus.KEEP_DECLARED),
        _seg(6, SegmentStatus.KEEP_ORIGINAL),
        _seg(7, SegmentStatus.SKIPPED),
    ])


def test_build_queue_excludes_keep_declared_and_keep_original() -> None:
    proj = _project()
    queue = TranslationEngine._build_queue(proj, None)
    assert queue == {1, 2}
    # 明确排除的三种保留/非重译状态
    assert 5 not in queue   # KEEP_DECLARED
    assert 6 not in queue   # KEEP_ORIGINAL
    assert 7 not in queue   # SKIPPED


def test_build_queue_only_uids_filter() -> None:
    proj = _project()
    # 仅请求 uid1（PENDING）与 uid5（KEEP_DECLARED）的子集
    queue = TranslationEngine._build_queue(proj, {1, 5})
    # KEEP_DECLARED 即便在 only_uids 中也不入队
    assert queue == {1}


def test_build_queue_empty_when_nothing_translatable() -> None:
    proj = Project(segments=[
        _seg(1, SegmentStatus.KEEP_ORIGINAL),
        _seg(2, SegmentStatus.REVIEWED),
    ])
    assert TranslationEngine._build_queue(proj, None) == set()


# ---------------------------------------------------------------------------
# BUGFIX(stop-flag-sticky)：停止标志不得跨 run() 粘连
#
# 用户实测（2026-10-24）：先点过一次「停止」，再点「重试失败项」时日志
# 立刻出现「重试 206 个失败项…… / 翻译已被用户停止」，一行都没翻就退出。
# 根因：UI 复用同一 engine 实例，而 _stop_requested 只在 __init__ 里初始化，
# stop() 置 True 后无人复位。
# ---------------------------------------------------------------------------


class _GoodProvider:
    """把英文原文翻成合法中文（能通过校验），并记录被调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def translate_batch(self, numbered_lines, profile, context=None,
                        allow_keep_declare=False, use_fallback_model=False):
        self.calls += 1
        return {uid: f"译文{uid}" for uid, _src in numbered_lines}


def _run_engine(engine, project, work_dir, **kw):
    from pytran.config import Config
    from pytran.validators import Validator
    events: list[tuple[str, dict]] = []
    engine.run(project, Config(work_dir=str(work_dir)), type("P", (), {})(),
               on_event=lambda n, d: events.append((n, d)), **kw)
    return [n for n, _ in events], events


def test_stop_flag_does_not_leak_into_next_run(work_dir) -> None:
    """停止一次后再 run()，必须真的翻译（回归：用户「重试失败项」秒停）。"""
    from pytran.validators import Validator

    provider = _GoodProvider()
    engine = TranslationEngine(provider, Validator())

    # 第一次：先排停，run() 应立即退出（N4 语义，一行不发）
    project1 = Project(segments=[_seg(1, SegmentStatus.PENDING)])
    engine.stop()
    names1, _ = _run_engine(engine, project1, work_dir)
    assert "translate_stopped" in names1, "run() 前的 stop 请求必须被尊重（N4/A7）"
    assert provider.calls == 0, "已排停时不应发出任何请求"

    # 第二次：同一个 engine 实例，没有再点停止 → 必须正常翻译
    project2 = Project(segments=[_seg(2, SegmentStatus.FAILED)])
    names2, _ = _run_engine(engine, project2, work_dir)
    assert "translate_stopped" not in names2, \
        "上一次的停止标志不得粘连到本次运行"
    assert provider.calls >= 1, "重试失败项必须真的发出请求"
    assert project2.segments[0].status == SegmentStatus.TRANSLATED


def test_stop_flag_cleared_even_when_provider_raises(work_dir) -> None:
    """provider 抛异常时也要清标志，否则后续运行全部秒停。"""
    from pytran.validators import Validator

    class Boom:
        calls = 0

        def translate_batch(self, *a, **k):
            Boom.calls += 1
            raise RuntimeError("network down")

    engine = TranslationEngine(Boom(), Validator())
    engine.stop()  # 先制造粘连
    # 第一次 run 被 stop 拦下，标志应在 finally 里清掉
    _run_engine(engine, Project(segments=[_seg(1, SegmentStatus.PENDING)]), work_dir)
    assert engine._stop_requested is False, "run() 出口必须复位停止标志"

    # 第二次：即便 provider 抛错，标志仍不得残留
    engine2 = TranslationEngine(Boom(), Validator())
    engine2._stop_requested = True  # 模拟粘连
    engine2._stop_requested = False
    names, _ = _run_engine(
        engine2, Project(segments=[_seg(2, SegmentStatus.FAILED)]), work_dir)
    assert "translate_stopped" not in names
    assert engine2._stop_requested is False
