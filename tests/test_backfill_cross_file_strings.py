"""strings 块跨文件错位（2026-10-25 事故，用户实测反馈）。

事故现象（用户原话）：
1. 「查询现在的工作区中，是否有这样一段 old "Take me to the Sylphir Island"
   我在审校界面的待翻译并没看到它。翻译后并写回后，我看到这段变成了
   new "{image=it_qu.webp} 波泽拉的水晶球"」
2. 「未翻译前，我人工数了有20个待翻译内容。在审校界面只看到19个」

根因：``backfill.match_translations`` 第 2 级（特征码归一化等值）**没有文件
约束**。``strings`` 块的 feature 是常量 ``translate chinese2 strings:``，同一
目标语言下所有文件的 strings 段 feature 完全相同，于是 ``feat_index`` 把所有
文件的 strings 段塌进同一个桶，``take()`` 按 FIFO 把 A 文件的译文派给 B 文件
的段。

现场数据：``tl/chinese2/images/dlc_aurora/dlc.rpy`` 的 43 对 strings 译文，
前 28 条被按位置写进 ``tl/chinese2/exp-1-3somes.rpy`` 的 28 条 strings 段。
第 28 条（该文件里唯一真正待译的 ``Take me to the Sylphir Island``）拿到了
dlc.rpy 的第 28 条译文 ``{image=it_qu.webp} 波泽拉的水晶球``，于是
``_emit_old_new_pair`` 判定 ``target != source`` → translated=True → 状态从
PENDING 翻成 REVIEWED，从「待翻译」筛选里消失 —— 人工数 20 条、界面只见 19 条。

修复：第 2/3 级按文件隔离（DECISION(feature-file-scope)）。
"""
from __future__ import annotations

from pytran.backfill import (
    TranslationRow,
    locator_file,
    match_translations,
    normalize_feature,
)
from pytran.models import Segment, SegmentStatus

# 真实常量：strings 块的特征码在 Ren'Py 里是固定的，与文件内容无关
STRINGS_FEATURE = "translate chinese2 strings:"


def _seg(uid: int, file: str, locator: str, feature: str,
         source: str, kind: str = "strings") -> Segment:
    return Segment(
        uid=uid, file=file, locator=locator, feature=feature, kind=kind,
        comment="", role="", source=source, status=SegmentStatus.PENDING,
    )


def _row(feature: str, locator: str, source: str, target: str,
         uid: int | None = None) -> TranslationRow:
    return TranslationRow(uid=uid, feature=feature, locator=locator,
                          source=source, target=target)


def test_strings_feature_is_constant_across_files() -> None:
    """前置事实：strings 块特征码归一化后与文件无关（这就是塌桶的成因）。"""
    a = normalize_feature(STRINGS_FEATURE)
    b = normalize_feature("translate chinese2 strings：")   # 全角冒号
    assert a == b == "translate chinese2 strings:"


def test_locator_file_parses_relative_path() -> None:
    """定位符能反推出文件相对路径（CSV 没有文件列，只能靠定位符）。"""
    assert locator_file("# game/tl/chinese2/exp-1-3somes.rpy:15425") == \
        "tl/chinese2/exp-1-3somes.rpy"
    assert locator_file("# game/a.rpy:7") == "a.rpy"
    assert locator_file("") == ""
    assert locator_file("没有定位符") == ""


def test_strings_do_not_leak_across_files() -> None:
    """核心回归：A 文件的 strings 译文绝不能落到 B 文件的 strings 段。

    复刻事故：dlc.rpy 已译、exp.rpy 待译，两者 feature 完全相同。
    """
    dlc_file = "tl/chinese2/images/dlc_aurora/dlc.rpy"
    exp_file = "tl/chinese2/exp-1-3somes.rpy"

    segs = [
        _seg(101, exp_file, f"# game/{exp_file}:100", STRINGS_FEATURE,
             "Take me to the Sylphir Island"),
    ]
    rows = [
        _row(STRINGS_FEATURE, f"# game/{dlc_file}:22717",
             "{image=it_qu.webp} CRYSTAL BALL FROM PORZELLA",
             "{image=it_qu.webp} 波泽拉的水晶球"),
    ]

    out = match_translations(segs, rows)
    assert out.matches == [], (
        "跨文件特征码匹配必须被拒绝：exp.rpy 的段拿到了 dlc.rpy 的译文")
    assert len(out.unmatched) == 1


def test_same_file_strings_still_match() -> None:
    """修复不能把同文件的 strings 匹配一起废掉（重导入要能回填）。"""
    f = "tl/chinese2/exp-1-3somes.rpy"
    segs = [
        _seg(201, f, f"# game/{f}:100", STRINGS_FEATURE, "Spell A"),
        _seg(202, f, f"# game/{f}:101", STRINGS_FEATURE, "Spell B"),
    ]
    rows = [
        _row(STRINGS_FEATURE, f"# game/{f}:100", "Spell A", "法术甲"),
        _row(STRINGS_FEATURE, f"# game/{f}:101", "Spell B", "法术乙"),
    ]
    out = match_translations(segs, rows)
    assert len(out.matches) == 2
    got = {m.segment.source: m.row.target for m in out.matches}
    assert got == {"Spell A": "法术甲", "Spell B": "法术乙"}
    for m in out.matches:
        assert m.segment.file == f
        assert m.method == "identity"  # v3.1：身份对位统一方法名


def test_strings_match_is_positional_within_same_file() -> None:
    """同文件内仍按顺序派发（本次修复不改动同文件内的既有语义）。"""
    f = "tl/chinese2/x.rpy"
    segs = [_seg(300 + i, f, f"# game/{f}:{i}", STRINGS_FEATURE, f"L{i}")
            for i in range(3)]
    rows = [_row(STRINGS_FEATURE, f"# game/{f}:{i}", f"L{i}", f"译{i}")
            for i in range(3)]
    out = match_translations(segs, rows)
    assert [m.segment.source for m in out.matches] == ["L0", "L1", "L2"]
    assert [m.row.target for m in out.matches] == ["译0", "译1", "译2"]


def test_path_prefix_mismatch_falls_back_to_filename() -> None:
    """历史快照里出现过「定位符少了 tl/chinese2/ 前缀」的数据。

    v3.1 语义裁决：身份 = (完整文件路径, 原文)。旧数据路径前缀缺失时
    **不再按文件名借用**——那是一层"猜测"（复盘 §3.2：有日志的猜测仍是
    猜测），同前缀不同文件名会串。新语义：对不上即 unmatched，交人工
    或由迁移工具重建 key。此用例从「必须命中」改为「必须不猜」。
    """
    segs = [_seg(401, "tl/chinese2/exp-1-3somes.rpy",
                 "# game/tl/chinese2/exp-1-3somes.rpy:50",
                 STRINGS_FEATURE, "Potion of Healing")]
    rows = [_row(STRINGS_FEATURE, "# game/exp-1-3somes.rpy:50",
                 "Potion of Healing", "治疗药水")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 0, (
        "路径前缀不同 = 文件身份不同，按身份语义不得命中（宁可 unmatched）")
    assert len(out.unmatched) == 1


def test_dialogue_feature_match_across_files_still_isolated() -> None:
    """对话段的 label 特征码通常带唯一 hash，但同名 label 跨文件也须隔离。"""
    segs = [
        _seg(501, "tl/chinese2/a.rpy", "# game/tl/chinese2/a.rpy:1",
             "translate chinese start_abc:", "Hello", kind="dialogue"),
        _seg(502, "tl/chinese2/b.rpy", "# game/tl/chinese2/b.rpy:9",
             "translate chinese start_abc:", "Hello", kind="dialogue"),
    ]
    rows = [_row("translate chinese start_abc:",
                 "# game/tl/chinese2/a.rpy:1", "Hello", "你好")]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    assert out.matches[0].segment.file == "tl/chinese2/a.rpy"


def test_similarity_stage_rejects_cross_file_same_locator() -> None:
    """第 3 级：定位符字符串相同但文件不同时，不得跨文件命中。"""
    segs = [
        _seg(601, "tl/chinese2/b.rpy", "# game/same:5",
             "f-b", "A long identical source line here", kind="dialogue"),
    ]
    rows = [_row("f-a", "# game/same:5",
                 "A long identical source line here", "译文")]
    out = match_translations(segs, rows)
    assert out.matches == []
    assert len(out.unmatched) == 1


def test_uid_match_requires_same_file_and_source() -> None:
    """uid 精确匹配仍是最高优先级，但**不再无条件相信** uid。

    上一版此用例断言「uid 含路径 hash，天然文件隔离」——**该前提是错的**：
    ``path_hash`` 只有 16 bit，实测 3000 个路径就有 59 次碰撞（生日界 ≈ 68）。
    两个不同文件共享 path_hash 时，seq1 段 uid 完全相同
    （``file_56.rpy`` 与 ``file_228.rpy`` 都得 2650210305），``take()`` 按
    列表顺序取第一个 → 谁先进工程谁拿到译文，另一文件被写入**别人的译文**。

    故 uid 级现在要求「同文件 + 原文可确认」，否则降级到特征码/相似度。
    """
    segs = [_seg(701, "tl/chinese2/b.rpy", "# game/tl/chinese2/b.rpy:3",
                 STRINGS_FEATURE, "Something")]
    rows = [_row(STRINGS_FEATURE, "# game/tl/chinese2/other.rpy:9",
                 "Completely different", "别的东西", uid=701)]
    out = match_translations(segs, rows)
    assert out.matches == [], "uid 相同但文件与原文都不同，必须拒绝"
    assert len(out.unmatched) == 1


def test_uid_match_still_wins_when_file_and_source_agree() -> None:
    """正常路径不受影响：同文件同原文 → 命中（v3.1 统一走身份级）。"""
    f = "tl/chinese2/b.rpy"
    segs = [_seg(702, f, f"# game/{f}:3", STRINGS_FEATURE, "Something")]
    rows = [_row(STRINGS_FEATURE, f"# game/{f}:3", "Something", "某物", uid=702)]
    out = match_translations(segs, rows)
    assert len(out.matches) == 1
    assert out.matches[0].method == "identity"  # v3.1：uid 降级为身份对位
    assert out.matches[0].segment.uid == 702


def test_uid_collision_between_two_files_does_not_cross_write() -> None:
    """path_hash 碰撞（真实可复现）：两文件同序号 uid 相同，不得串译。

    ``tl/chinese2/file_56.rpy`` 与 ``tl/chinese2/file_228.rpy`` 的
    path_hash 都是 40439，seq1 uid 都是 2650210305。
    """
    from pytran.uids import make_uid, path_hash

    pa, pb = "tl/chinese2/file_56.rpy", "tl/chinese2/file_228.rpy"
    assert path_hash(pa) == path_hash(pb), "前提：这两个路径 hash 确实碰撞"
    assert make_uid(pa, 1) == make_uid(pb, 1)

    seg_a = _seg(make_uid(pa, 1), pa, f"# game/{pa}:1",
                 STRINGS_FEATURE, "FIREBALL")
    seg_b = _seg(make_uid(pb, 1), pb, f"# game/{pb}:1",
                 STRINGS_FEATURE, "ICE SHIELD")
    rows = [_row(STRINGS_FEATURE, f"# game/{pa}:1", "FIREBALL", "火球术",
                 uid=make_uid(pa, 1))]

    for order in ([seg_a, seg_b], [seg_b, seg_a]):
        out = match_translations(order, rows)
        assert len(out.matches) == 1
        m = out.matches[0]
        assert m.segment.file == pa, (
            f"译文必须落在 {pa}，实际落在 {m.segment.file}（顺序={order[0].file}）")
        assert m.segment.source == "FIREBALL"


def test_full_dlc_into_exp_scenario_counts_all_unmatched() -> None:
    """复刻真实规模：dlc.rpy 28 条已译 + exp.rpy 28 条待译 → 0 命中 28 未匹配。

    修复前这里是 matches=28 / unmatched=0，且 28 条译文全部错位。
    """
    dlc_file = "tl/chinese2/images/dlc_aurora/dlc.rpy"
    exp_file = "tl/chinese2/exp-1-3somes.rpy"
    segs = [_seg(1000 + i, exp_file, f"# game/{exp_file}:{100 + i}",
                 STRINGS_FEATURE, f"EXP LINE {i}") for i in range(28)]
    rows = [_row(STRINGS_FEATURE, f"# game/{dlc_file}:{200 + i}",
                 f"DLC LINE {i}", f"dlc译{i}") for i in range(28)]
    out = match_translations(segs, rows)
    assert len(out.matches) == 0
    assert len(out.unmatched) == 28
    # 反向确认：没有一条 exp 段被写入 dlc 译文
    for s in segs:
        assert s.target is None
