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
