"""W3 回填与导出熔断测试（设计文档 §6.2 / §6.3）。

覆盖：
* §6.2 兜底链四档：uid 精确 / 特征码归一化 / 相似度≥0.92 同定位符 / 全失败→unmatched；
* §6.3 导出熔断：untranslated>0、failed>0、tag_check>0、match_rate<0.95 各自触发
  ``ExportBlocked``；全部满足则通过。
"""

from __future__ import annotations

import pytest

from pytran.backfill import (
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
# DECISION(segkey-identity, 2026-10-26)：三级兜底链已退役（复盘证明每个
# "高级别"的正确性依赖未验证的外部前提）。match_translations 现为薄包装，
# 转调 identity.resolve_batch 身份精确对位，method 统一为 "identity"。
# 原「uid 主键/优先级/相似度」用例按新语义重写；uid 参数被忽略。


def test_match_by_uid_exact():
    """同文件同原文 → 身份命中（uid 字段不再参与判定）。"""
    segs = [_seg(10, "say", "a.rpy:5", "hello")]
    rows = [_row(uid=10, feature="other", locator="zzz", source="hello", target="你好")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    m = out.matches[0]
    assert m.method == "identity"
    assert m.segment.uid == 10
    assert out.unmatched == []


def test_match_uid_takes_precedence_over_feature():
    """原文一致的行命中原文一致的段——不存在"uid 盲胜"。
    （原用例语义已随 uid 盲胜一并证伪，见 v3.1 任务书 §5.4。）"""
    segs = [
        _seg(10, "say", "a.rpy:5", "hello"),
        _seg(20, "say", "a.rpy:9", "world"),
    ]
    rows = [_row(uid=10, feature="say", locator="a.rpy:9", source="hello", target="X")]
    out = match_translations(segs, rows)
    assert out.matches[0].method == "identity"
    assert out.matches[0].segment.uid == 10


def test_uid_hit_rejected_when_source_is_a_different_line():
    """uid 命中但原文已是另一句话 → 拒绝，不得错误覆盖。"""
    segs = [
        _seg(10, "say", "a.rpy:5", "hello"),
        _seg(20, "say", "a.rpy:9", "world"),
    ]
    rows = [_row(uid=10, feature="say", locator="a.rpy:9", source="world", target="X")]
    out = match_translations(segs, rows)
    # 不得落在 uid=10（那是 hello）；应落到原文一致的 uid=20
    assert all(m.segment.source != "hello" for m in out.matches)
    hit = [m for m in out.matches if m.segment.uid == 20]
    assert hit and hit[0].method == "identity"


def test_uid_matching_opt_out_for_untrusted_source():
    """uid 不可信来源：身份对位天然不依赖 uid，行为等同。"""
    segs = [_seg(10, "say", "a.rpy:5", "hello")]
    rows = [_row(uid=999, feature="say", locator="a.rpy:5",
                 source="hello", target="你好")]
    out = match_translations(segs, rows, use_uid_matching=False)
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"


# ----------------------------- §6.2 特征码归一化 -----------------------------

def test_match_by_normalized_feature():
    """v3.1：特征码不再参与匹配（身份 = 文件+原文），行为按命中数验证。"""
    segs = [_seg(11, "say''old", "a.rpy:5", "hello")]
    rows = [_row(feature='say"old', locator="elsewhere", source="hello", target="你好")]
    out = match_translations(segs, rows, use_uid_matching=True)
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"
    assert out.matches[0].segment.uid == 11


# ----------------------------- §6.2 相似度兜底（已删除） -----------------------------

def test_match_by_similarity_same_locator():
    """v3.1：相似度兜底已整体移除（复盘 §3.2「带日志的猜测仍是猜测」）。
    同文件同原文仍命中（身份级），method 为 identity 而非 similarity。"""
    segs = [_seg(12, "say", "a.rpy:5", "the quick brown fox jumps over")]
    rows = [_row(
        feature="x", locator="a.rpy:5",
        source="the quick brown fox jumps over", target="快速的棕色狐狸跳过",
    )]
    out = match_translations(segs, rows, use_uid_matching=True)
    assert len(out.matches) == 1
    m = out.matches[0]
    assert m.method == "identity"
    assert m.segment.uid == 12


def test_no_similarity_across_different_locator():
    """v3.1 语义裁决：相似度级已删除。row 定位符指向 a.rpy:99、段文件
    为 a.rpy——locator_file 反推的文件一致且原文精确相等 → 按身份命中
    （这不是相似度兜底，是身份精确对位；原用例防御的"跨定位符相似度
    猜测"在无相似度级的构造下不可能发生）。"""
    segs = [_seg(13, "say", "a.rpy:5", "the quick brown fox jumps over")]
    rows = [_row(feature="x", locator="a.rpy:99", source="the quick brown fox jumps over", target="Y")]
    out = match_translations(segs, rows, use_uid_matching=True)
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"
    assert out.matches[0].segment.uid == 13


def test_similarity_below_threshold_is_unmatched():
    segs = [_seg(14, "say", "a.rpy:5", "completely different sentence here")]
    rows = [_row(feature="x", locator="a.rpy:5", source="totally unrelated text now", target="Z")]
    out = match_translations(segs, rows, use_uid_matching=True)
    assert out.unmatched == [rows[0]]


# ----------------------------- §6.2 全失败 -----------------------------

def test_all_unmatched_when_nothing_fits():
    segs = [_seg(15, "say", "a.rpy:5", "hello")]
    rows = [
        _row(uid=999, feature="nope", locator="a.rpy:1", source="zzz", target="1"),
        _row(uid=888, feature="nada", locator="b.rpy:2", source="qqq", target="2"),
    ]
    out = match_translations(segs, rows, use_uid_matching=True)
    assert out.matches == []
    assert len(out.unmatched) == 2


def test_no_double_consume_same_segment():
    segs = [_seg(16, "say", "a.rpy:5", "hello")]
    rows = [
        _row(uid=16, feature="say", locator="a.rpy:5", source="hello", target="A"),
        _row(uid=None, feature="say", locator="a.rpy:5", source="hello", target="B"),
    ]
    out = match_translations(segs, rows, use_uid_matching=True)
    # 一个段只被消费一次；第二条同原文行进 unmatched（桶耗尽不猜）
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"
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


# ----------------------------- v3.1 §2.5 构造性不变量 -----------------------------

def test_keep_original_and_reviewed_excluded_from_fuse():
    """用户裁决 2026-10-26：保留原文与人工审校通过的行不进熔断检测。

    * KEEP_ORIGINAL 天然无译文——早先被误报为 untranslated（用户实测：
      审校待翻译 0 条、熔断未翻译 1 条，即 screens.rpy 空格保留段）；
    * REVIEWED 是人工终态裁定，词法校验（tag_check）不得推翻人工结论
      （转义引号风格差异等合法翻译曾被误拦）。
    """
    # KEEP_ORIGINAL 无译文：不得计入 untranslated
    proj_ko = _project([
        _seg(10, "say", "a.rpy:10", " ", status=SegmentStatus.KEEP_ORIGINAL),
        _seg(11, "say", "a.rpy:11", "hi", target="你好", status=SegmentStatus.TRANSLATED),
    ])
    assert evaluate_export_fuse(proj_ko) == {}

    # REVIEWED 行即使 tag 不一致（合法的引号风格转换）也不进 tag_check
    proj_rv = _project([
        _seg(12, "say", "a.rpy:12", 'He said \\"hi\\" to me',
             target='他对我说『你好』', status=SegmentStatus.REVIEWED),
    ])
    assert evaluate_export_fuse(proj_rv) == {}

    # 对照：机器产出（TRANSLATED）仍受检——用结构性转义做反例
    # （引号转义已按等价规则放行，不再构成 tag_check）
    proj_tr = _project([
        _seg(13, "say", "a.rpy:13", "line1 \\n line2",
             target="第一行\n第二行", status=SegmentStatus.TRANSLATED),
    ])
    d = evaluate_export_fuse(proj_tr)
    assert "tag_check" in d
    assert "13" in d["tag_check"][0]

    # KEEP_DECLARED（AI 申报、待人工裁定）不是终态，仍受检
    proj_kd = _project([
        _seg(14, "say", "a.rpy:14", "hi", status=SegmentStatus.KEEP_DECLARED),
    ])
    d = evaluate_export_fuse(proj_kd)
    assert "untranslated" in d


# ----------------------------- 引号转义等价（BUGFIX quote-esc-equivalence）-------

def test_quote_esc_to_cjk_quotes_is_ok():
    """原文 \\" → 译文中文引号：合法翻译，不得报 tag_check。

    用户实测误报样例：'He said \\"hi\\"' → 『你好』——6 条同类全被
    旧的字面计数误拦。"""
    from pytran.validators import tags_ok, tag_messages

    assert tags_ok('He said \\"hi\\" to me', "他对我说『你好』")
    assert tags_ok('Recall \\"The phrase\\" here', "回想『这句话』")
    # 反向：原文用中文引号、译文用 \\" 转义，同样放行
    assert tags_ok("他说『你好』", 'He said \\"hi\\"')


def test_quote_esc_imbalance_still_fails():
    """引号转义完全退出计数校验（最终版口径）。

    BUGFIX(quote-esc-direction, 2026-10-26)：引号不改变 Ren'Py 文本结构，
    数量不等/凭空出现/风格改写均为合法修辞，全部放行（用户实测 10 条
    误报全属此类）。结构性转义（\\n 等）仍严格——见上一用例。"""
    from pytran.validators import tags_ok

    q = '\\"'   # 字面两字符：反斜杠 + 双引号

    assert tags_ok('say something plain', f'说{q}强调{q}')       # 译文凭空引号 → 放行
    assert tags_ok(f'say {q}quoted{q}', "纯文本无引号")          # 引号"丢失" → 放行
    assert tags_ok(f'say {q}a{q} and {q}b{q}', "说甲和乙")       # 数量不等 → 放行
    # 对照：结构转义不受此放宽影响
    assert not tags_ok("line1 \\n line2", "第一行\n第二行")


def test_structural_esc_still_strict():
    """结构性转义（\\n 等）不放宽：计数不等仍要报。

    uid=6274 真实案例：原文含字面 \\n，译文把它变成真实换行 → 仍应检出
    （这可能是真问题，需人工确认）。"""
    from pytran.validators import tags_ok

    assert not tags_ok("line1 \\n line2", "第一行\n第二行")  # 字面 \n vs 真实换行
    assert tags_ok("line1 \\n line2", "第一行 \\n 第二行")   # 保留字面 \n → OK


def test_identity_violation_raises_on_source_mismatch():
    """任务书验收 4.1-6：身份命中但原文不一致必须在构造点显式爆炸。

    这条防线保证：任何未来改动若重新引入模糊命中（如相似度兜底复活、
    按位置盲取桶段），都会在此异常中暴露，而不是静默错位写回游戏文件。
    """
    from pytran.identity import resolve_batch, IdentityViolation
    from pytran import identity as identity_mod
    from pytran.models import Segment

    seg = Segment(
        uid=1, file="a.rpy", locator="# game/a.rpy:1", feature="f",
        kind="dialogue", comment="", role="", source="hello",
    )

    class Row:
        file = "a.rpy"
        source = "hello"
        target = "X"

    # 正常路径不炸
    out = resolve_batch([seg], [Row()])
    assert len(out.matches) == 1

    # 模拟未来回归：resolve_next 放弃等值筛选（相似度兜底/位置盲取复活）
    seg.source = "hacked"   # 段原文与 row 不再一致
    orig = identity_mod.IdentityResolver.resolve_next

    def fuzzy_resolve(self, file, source):
        for key, entry in self._index._buckets.items():
            if key.startswith("v1\x1fa.rpy\x1f"):
                for s in entry.segments:
                    if s.uid not in self._used_uids:
                        self._used_uids.add(s.uid)
                        return s
        return None

    identity_mod.IdentityResolver.resolve_next = fuzzy_resolve
    try:
        with pytest.raises(IdentityViolation):
            resolve_batch([seg], [Row()])
    finally:
        identity_mod.IdentityResolver.resolve_next = orig

    # 恢复后：篡改的段原文不匹配 → unmatched（不猜，不炸）
    out2 = resolve_batch([seg], [Row()])
    assert out2.matches == [] and len(out2.unmatched) == 1