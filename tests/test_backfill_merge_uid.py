"""uid 匹配与稳定 uid 分配（2026-10-24 事故修复，用户裁决方案）。

事故：合并导入按 uid 对位，但 build_project 每次把 uid 从 1 重编 →
新旧 uid 撞车（旧 uid=5 配到新 uid=5 完全不同的段）→ 写回后 15 个
文件译文全部张冠李戴。

修复（用户裁决）：uid 保持唯一主键地位，分配改为「文件路径 hash +
文件内序号」（pytran.uids.make_uid）——同文件同段跨导入 uid 一致，
uid 匹配从此安全。详见 docs/BUGFIX-merge-uid-collision.md。
"""
from __future__ import annotations

from pytran.backfill import TranslationRow, match_translations
from pytran.models import Segment, SegmentStatus
from pytran.project import build_project


def _seg(uid: int, file: str, feature: str, source: str) -> Segment:
    return Segment(
        uid=uid, file=file, locator=f"# game/{file}:{uid}",
        feature=feature, kind="dialogue", comment="", role="",
        source=source, status=SegmentStatus.PENDING,
    )


def test_uid_matching_enabled_by_default() -> None:
    """v3.1：身份对位（文件+原文）成为唯一匹配语义，uid 参数被忽略。
    原「uid 唯一主键」语义已随复盘证伪退役。"""
    segs = [_seg(10, "say", "a.rpy:5", "hello")]
    rows = [TranslationRow(uid=10, feature="other", locator="zzz",
                           source="hello", target="你好")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"


def test_uid_collision_prevented_by_stable_allocation() -> None:
    """核心回归：稳定 uid 分配下，不同文件同序号段 uid 不同，且同文件
    同段跨导入 uid 一致——2026-10-24 的「旧 uid=5 配到新 uid=5」在分配层
    即被杜绝。"""
    fa, fb = "tl/chinese2/common.rpy", "tl/chinese2/exp-wos.rpy"
    first = [_seg(0, fa, "f", f"c{i}") for i in range(6)] + \
            [_seg(0, fb, "f", f"w{i}") for i in range(6)]
    build_project(first)
    # common 第 5 段与 exp-wos 第 5 段 uid 不同（hash 槽隔离）
    assert first[4].uid != first[6 + 4].uid
    # 两次导入间 uid 稳定
    second = [_seg(0, fa, "f", f"c{i}") for i in range(6)] + \
             [_seg(0, fb, "f", f"w{i}") for i in range(6)]
    build_project(second)
    assert first[4].uid == second[4].uid
    assert first[6 + 4].uid == second[6 + 4].uid


def test_uid_matching_can_be_disabled_for_untrusted_source() -> None:
    """v3.1：身份对位天然不依赖 uid，"关闭 uid"开关被忽略，行为等同。"""
    segs = [_seg(10, "say", "say", "hello")]
    rows = [TranslationRow(uid=999, feature="say", locator="# game/say:5",
                           source="hello", target="你好")]
    out = match_translations(segs, rows, use_uid_matching=False)
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"


def test_feature_matching_still_works() -> None:
    """v3.1：特征码不参与匹配；同文件同原文仍按身份命中。"""
    segs = [_seg(99, "a.rpy", "translate chinese start_the_game_2544aaf2:",
                 "Your heart begins to race...")]
    rows = [TranslationRow(
        uid=1, feature="translate chinese start_the_game_2544aaf2:",
        locator="# game/a.rpy:172",
        source="Your heart begins to race...",
        target="你的心开始加速……")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"


def test_same_source_different_file_no_cross_match() -> None:
    """一条译文只占一个段（不得同时写两个文件）。"""
    segs = [
        _seg(1, "a.rpy", "translate chinese t1:", "Same line"),
        _seg(2, "b.rpy", "translate chinese t1:", "Same line"),
    ]
    rows = [TranslationRow(uid=1, feature="translate chinese t1:",
                           locator="# game/a.rpy:1", source="Same line",
                           target="译文A")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
