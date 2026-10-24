"""BUGFIX(merge-uid-collision)：合并导入不得按 uid 跨导入对位。

用户实测 2026-10-24：写回后「合并导回」→ match_translations 第一级
uid 精确匹配把旧工程 uid=5 的译文写到新工程 uid=5（完全不同的另一段）
→ 写回后 15 个文件译文全部张冠李戴（已从事发前 bak/game 快照恢复）。

修复：
* match_translations 的 uid 级匹配默认关闭（use_uid_matching=False）；
* ui._merge_import 先用 (文件, 原文) 精确对位，特征码/相似度只兜底。
"""
from __future__ import annotations

from pytran.backfill import TranslationRow, match_translations
from pytran.models import Segment, SegmentStatus


def _seg(uid: int, file: str, feature: str, source: str) -> Segment:
    return Segment(
        uid=uid, file=file, locator=f"# game/{file}:{uid}",
        feature=feature, kind="dialogue", comment="", role="",
        source=source, status=SegmentStatus.PENDING,
    )


def test_uid_matching_disabled_by_default() -> None:
    """默认不得按 uid 精确匹配——uid 跨导入无语义（序号每次从 1 重编）。

    特征码/定位符/原文全都不同，唯一相同的是 uid——旧实现会凭 uid
    把 A 文件的译文写到 B 文件完全无关的段上（用户实测 2026-10-24）。
    """
    segs = [_seg(5, "b.rpy", "translate chinese other_label_999:",
                 "Totally different line")]
    rows = [TranslationRow(uid=5, feature="translate chinese target_label_123:",
                           locator="# game/a.rpy:5",
                           source="Original English line",
                           target="这是完全另一句的译文")]
    out = match_translations(segs, rows)
    assert out.matches == [], f"uid 撞车被错误匹配: {out.matches}"
    assert len(out.unmatched) == 1


def test_uid_matching_can_be_enabled_for_same_session() -> None:
    """同会话内 CSV 回填可显式开启 uid 匹配（导入后未重编的场景）。"""
    segs = [_seg(5, "a.rpy", "translate chinese t5:", "Original English line")]
    rows = [TranslationRow(uid=5, feature="translate chinese t5:",
                           locator="# game/a.rpy:5",
                           source="Original English line",
                           target="正确译文")]
    out = match_translations(segs, rows, use_uid_matching=True)
    assert len(out.matches) == 1
    assert out.matches[0].method == "uid"
    assert out.matches[0].segment.target is None  # 匹配不写回，调用方负责


def test_feature_matching_still_works() -> None:
    """特征码（label）匹配不受影响——跨导入的正确主键。"""
    segs = [_seg(99, "a.rpy", "translate chinese start_the_game_2544aaf2:",
                 "Your heart begins to race...")]
    rows = [TranslationRow(
        uid=1,  # 旧 uid，故意不同
        feature="translate chinese start_the_game_2544aaf2:",
        locator="# game/a.rpy:172",
        source="Your heart begins to race...",
        target="你的心开始加速……")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    assert out.matches[0].method == "feature"


def test_same_source_different_file_no_cross_match() -> None:
    """同源不同文件：特征码相同但文件不同——不应错配（label hash 唯一，
    此处构造相同 feature 模拟碰撞，验证 consumed 机制不串写）。"""
    segs = [
        _seg(1, "a.rpy", "translate chinese t1:", "Same line"),
        _seg(2, "b.rpy", "translate chinese t1:", "Same line"),
    ]
    rows = [TranslationRow(uid=1, feature="translate chinese t1:",
                           locator="# game/a.rpy:1", source="Same line",
                           target="译文A")]
    out = match_translations(segs, rows)
    # 一条译文只占一个段（不得同时写两个文件）
    assert len(out.matches) == 1
