"""W3 回填与导出熔断测试（设计文档 §6.2 / §6.3）。

覆盖：
* §6.2 兜底链四档：uid 精确 / 特征码归一化 / 相似度≥0.92 同定位符 / 全失败→unmatched；
* §6.3 导出熔断：untranslated>0、failed>0、tag_check>0、match_rate<0.95 各自触发
  ``ExportBlocked``；全部满足则通过。
"""

from __future__ import annotations

import pytest

from pytran.backfill import (
    SIMILARITY_THRESHOLD,
    TranslationMatch,
    TranslationRow,
    evaluate_export_fuse,
    match_translations,
    normalize_feature,
)
from pytran.models import (
    Project,
    Segment,
    SegmentStatus,
)
from pytran.interfaces import ExportBlocked


def _seg(uid, feature, locator, source, target="", status=SegmentStatus.PENDING):
    return Segment(
        uid=uid, file="a.rpy", locator=locator, feature=feature,
        kind="say", comment="", role="say", source=source,
        target=target, status=status, attempts=0, error=None, indent=4,
    )


def _row(uid=None, feature="", locator="", source="", target="T"):
    return TranslationRow(uid=uid, feature=feature, locator=locator, source=source, target=target)


# ----------------------------- §6.2 归一化 -----------------------------

def test_normalize_feature_strips_and_unifies_quotes_and_colons():
    assert normalize_feature("  old''text  ") == 'old"text'
    assert normalize_feature('  say""x  ') == 'say"x'
    assert normalize_feature("  说：hello  ") == "说:hello"
    assert normalize_feature("a   b\tc") == "a b c"


# ----------------------------- §6.2 uid 精确 -----------------------------

def test_match_by_uid_exact():
    segs = [_seg(10, "say", "a.rpy:5", "hello")]
    rows = [_row(uid=10, feature="other", locator="zzz", source="x", target="你好")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    m = out.matches[0]
    assert m.method == "uid"
    assert m.segment.uid == 10
    assert out.unmatched == []


def test_match_uid_takes_precedence_over_feature():
    # 同一 feature 匹配到另一 uid，但 uid 命中优先
    segs = [
        _seg(10, "say", "a.rpy:5", "hello"),
        _seg(20, "say", "a.rpy:9", "world"),
    ]
    rows = [_row(uid=10, feature="say", locator="a.rpy:9", source="world", target="X")]
    out = match_translations(segs, rows)
    assert out.matches[0].method == "uid"
    assert out.matches[0].segment.uid == 10


# ----------------------------- §6.2 特征码归一化 -----------------------------

def test_match_by_normalized_feature():
    # §6.2：'' 与 "" 都归一化为 "，因此 say''old 与 say"old 等值
    segs = [_seg(11, "say''old", "a.rpy:5", "hello")]
    rows = [_row(feature='say"old', locator="elsewhere", source="hello", target="你好")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    assert out.matches[0].method == "feature"
    assert out.matches[0].segment.uid == 11


# ----------------------------- §6.2 相似度兜底 -----------------------------

def test_match_by_similarity_same_locator():
    segs = [_seg(12, "say", "a.rpy:5", "the quick brown fox jumps over")]
    rows = [_row(
        feature="x", locator="a.rpy:5",
        source="the quick brown fox jumps over", target="快速的棕色狐狸跳过",
    )]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    m = out.matches[0]
    assert m.method == "similarity"
    assert m.segment.uid == 12


def test_no_similarity_across_different_locator():
    segs = [_seg(13, "say", "a.rpy:5", "the quick brown fox jumps over")]
    rows = [_row(feature="x", locator="a.rpy:99", source="the quick brown fox jumps over", target="Y")]
    out = match_translations(segs, rows)
    assert out.matches == []
    assert len(out.unmatched) == 1


def test_similarity_below_threshold_is_unmatched():
    segs = [_seg(14, "say", "a.rpy:5", "completely different sentence here")]
    rows = [_row(feature="x", locator="a.rpy:5", source="totally unrelated text now", target="Z")]
    out = match_translations(segs, rows)
    assert out.unmatched == [rows[0]]


# ----------------------------- §6.2 全失败 -----------------------------

def test_all_unmatched_when_nothing_fits():
    segs = [_seg(15, "say", "a.rpy:5", "hello")]
    rows = [
        _row(uid=999, feature="nope", locator="a.rpy:1", source="zzz", target="1"),
        _row(uid=888, feature="nada", locator="b.rpy:2", source="qqq", target="2"),
    ]
    out = match_translations(segs, rows)
    assert out.matches == []
    assert len(out.unmatched) == 2


def test_no_double_consume_same_segment():
    segs = [_seg(16, "say", "a.rpy:5", "hello")]
    rows = [
        _row(uid=16, feature="say", locator="a.rpy:5", source="hello", target="A"),
        _row(uid=None, feature="say", locator="a.rpy:5", source="hello", target="B"),
    ]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    assert out.matches[0].method == "uid"
    assert len(out.unmatched) == 1


# ----------------------------- §6.3 导出熔断 -----------------------------

def _project(segments):
    return Project(version=3, app="pytran", files=["a.rpy"], segments=segments,
                   term_profile="profile.json", game_root="", stats=None)


def test_fuse_blocks_on_untranslated():
    proj = _project([_seg(1, "say", "a.rpy:1", "hi")])  # 无 target
    d = evaluate_export_fuse(proj)
    assert "untranslated" in d
    with pytest.raises(ExportBlocked):
        from pytran.backfill import check_export_fuse
        check_export_fuse(proj)


def test_fuse_blocks_on_failed():
    proj = _project([_seg(2, "say", "a.rpy:2", "hi", target="X", status=SegmentStatus.FAILED)])
    d = evaluate_export_fuse(proj)
    assert "failed" in d


def test_fuse_blocks_on_tag_check():
    # source 有 1 个插值 {name}，target 没有 → tags_ok 失败
    seg = _seg(3, "say", "a.rpy:3", "hello {name}", target="你好")
    proj = _project([seg])
    d = evaluate_export_fuse(proj)
    assert "tag_check" in d


def test_fuse_blocks_on_match_rate():
    seg = _seg(4, "say", "a.rpy:4", "hi", target="你好")
    proj = _project([seg])
    d = evaluate_export_fuse(proj, match_rate=0.90)
    assert "match_rate" in d


def test_fuse_passes_when_clean():
    seg = _seg(5, "say", "a.rpy:5", "hello {name}", target="你好 {name}")
    proj = _project([seg])
    d = evaluate_export_fuse(proj)
    assert d == {}
    from pytran.backfill import check_export_fuse
    check_export_fuse(proj)  # 不应抛


def test_skipped_segments_excluded_from_fuse():
    proj = _project([
        _seg(6, "say", "a.rpy:6", "hi", status=SegmentStatus.SKIPPED),
        _seg(7, "say", "a.rpy:7", "hi", target="你好", status=SegmentStatus.TRANSLATED),
    ])
    d = evaluate_export_fuse(proj)
    assert d == {}
