"""导入 / 合并导入 / 写回后重导入 —— 多场景错位与错误覆盖回归套件。

对应需求（2026-10-25）：
  「设计多种测试方案，测试导入，合并导入，写入后导入等流程。
    是否会再出现错位，错误覆盖等bug」

本文件把「译文是否落在正确的原文上」作为唯一验收标准。历史事故的共同
特征是：**匹配数量看起来完全正常**（``matches=666 unmatched=0``），但译文
落错了段/落错了文件。因此这里的断言一律检查
``m.segment.file`` / ``m.segment.source`` / ``m.method``，
**绝不**只断言 ``len(matches)``。

场景矩阵
--------
A. 全新导入        : 首次解析 → 分配 uid → 译文按 uid 落位
B. 合并导入（不变）: 二次解析内容不变 → uid 全部对齐，零漂移
C. 合并导入（插入）: 文件中间插入一行 → uid 后移，旧译文不得错位
D. 合并导入（删除）: 删除一行 → uid 前移，旧译文不得错位
E. 合并导入（乱序）: 段落重排 → 旧译文必须跟着原文走，不能跟着序号走
F. 合并导入（改写）: 原文被改 → 不得把旧译文套到新句子上
G. 写回后重导入    : regenerate → 重新 parse → 再合并导入，译文必须原样
H. 多文件 strings  : 相同特征码跨文件，不得串译
I. path_hash 碰撞  : 两文件同序号 uid 相同，不得跨文件覆盖
J. CSV 往返        : 导出 CSV → 改 → 导入，不得错位
K. 端到端熔断      : 错位发生后写回前必须被拦住
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from pytran.backfill import (
    TranslationRow,
    match_translations,
    evaluate_export_fuse,
)
from pytran.models import Segment, SegmentStatus
from pytran.project import build_project
from pytran.uids import assign_uids, make_uid, path_hash

STRINGS_FEATURE = "translate chinese2 strings:"


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #
def _seg(uid: int, file: str, locator: str, source: str,
         feature: str = "say", kind: str = "dialogue") -> Segment:
    return Segment(
        uid=uid, file=file, locator=locator, feature=feature, kind=kind,
        comment="", role="", source=source,
    )


def _row(source: str, target: str, *, file: str, line: int,
         feature: str = "say", uid: int | None = None,
         kind: str = "dialogue") -> TranslationRow:
    return TranslationRow(
        uid=uid, feature=feature,
        locator=f"# game/{file}:{line}",
        source=source, target=target,
    )


def _make_segments(file: str, lines: list[str],
                   feature: str = "say", kind: str = "dialogue",
                   start_line: int = 1) -> list[Segment]:
    """按文件行序构造段（uid 先留 0，由 assign_uids 分配）。"""
    return [
        _seg(0, file, f"# game/{file}:{start_line + i}", src,
             feature=feature, kind=kind)
        for i, src in enumerate(lines)
    ]


def _assign(segs: list[Segment]) -> list[Segment]:
    """按 ``assign_uids`` 的真实签名（{路径: [段]}）分配 uid，返回同一列表。"""
    by_file: dict[str, list[Segment]] = {}
    for s in segs:
        by_file.setdefault(s.file, []).append(s)
    assign_uids(by_file)
    return segs


def _apply_translations(segs: list[Segment], mapping: dict[str, str]) -> None:
    """把 {原文: 译文} 应用到段上并置 TRANSLATED。"""
    for s in segs:
        if s.source in mapping:
            s.target = mapping[s.source]
            s.status = SegmentStatus.TRANSLATED


def _as_rows(segs: list[Segment]) -> list[TranslationRow]:
    """模拟 ``_merge_import`` 的取行方式：只取已译且译文非空的行。"""
    return [
        TranslationRow(uid=s.uid, feature=s.feature, locator=s.locator,
                       source=s.source, target=s.target or "")
        for s in segs
        if s.status in (SegmentStatus.TRANSLATED, SegmentStatus.REVIEWED)
        and s.target and s.target.strip()
    ]


def _assert_all_landed_correctly(segs: list[Segment],
                                 outcome) -> None:
    """核心断言：每条命中的译文都必须落在**原文相同**的段上。"""
    for m in outcome.matches:
        assert (m.segment.source or "").strip() == (m.row.source or "").strip(), (
            f"错位：原文 {m.row.source!r} 的译文 {m.row.target!r} 落到了"
            f" uid={m.segment.uid} 的 {m.segment.source!r} 上"
            f"（方式={m.method}, 文件={m.segment.file}）"
        )
        assert m.segment.file == _row_file(m.row), (
            f"跨文件覆盖：译文来自 {_row_file(m.row)} 却写进 {m.segment.file}"
        )


def _row_file(row: TranslationRow) -> str:
    from pytran.backfill import locator_file
    return locator_file(row.locator)


# --------------------------------------------------------------------------- #
# A. 全新导入
# --------------------------------------------------------------------------- #
def test_A_fresh_import_assigns_uids_and_places_translations() -> None:
    """首次导入：uid 由 (路径 hash, 序号) 分配，译文按 uid 正确落位。"""
    f = "tl/chinese2/ch1.rpy"
    segs = _make_segments(f, ["Hello", "World", "Goodbye"])
    _assign(segs)

    assert len({s.uid for s in segs}) == 3, "同文件内 uid 必须互异"
    assert [s.uid for s in segs] == [
        make_uid(f, 1), make_uid(f, 2), make_uid(f, 3)]

    _apply_translations(segs, {"Hello": "你好", "World": "世界",
                               "Goodbye": "再见"})

    # 重新导入同一内容 → 译文全部按 uid 命中且原文一致
    fresh = _make_segments(f, ["Hello", "World", "Goodbye"])
    _assign(fresh)
    out = match_translations(fresh, _as_rows(segs))

    assert len(out.matches) == 3
    assert out.unmatched == []
    assert {m.method for m in out.matches} == {"identity"}
    _assert_all_landed_correctly(fresh, out)


def test_A_uid_is_stable_across_independent_imports() -> None:
    """同一文件重复导入（内容不变）→ uid 完全一致，这是合并导入的基础。"""
    f = "tl/chinese2/ch2.rpy"
    a = _make_segments(f, [f"L{i}" for i in range(20)])
    b = _make_segments(f, [f"L{i}" for i in range(20)])
    _assign(a)
    _assign(b)
    assert [s.uid for s in a] == [s.uid for s in b]


# --------------------------------------------------------------------------- #
# B. 合并导入：内容不变
# --------------------------------------------------------------------------- #
def test_B_merge_import_unchanged_is_lossless() -> None:
    """内容不变时合并导入必须 100% 按 uid 命中，且无一错位。"""
    f = "tl/chinese2/ch3.rpy"
    old = _make_segments(f, ["One", "Two", "Three", "Four"])
    _assign(old)
    _apply_translations(old, {"One": "一", "Two": "二",
                              "Three": "三", "Four": "四"})

    new = _make_segments(f, ["One", "Two", "Three", "Four"])
    _assign(new)
    out = match_translations(new, _as_rows(old))

    assert len(out.matches) == 4
    assert out.unmatched == []
    assert {m.method for m in out.matches} == {"identity"}
    _assert_all_landed_correctly(new, out)
    assert [m.segment.target for m in
            sorted(out.matches, key=lambda m: m.segment.uid)] == [
        None, None, None, None]  # 匹配不直接写 target，由调用方写


# --------------------------------------------------------------------------- #
# C. 合并导入：中间插入一行（uid 整体后移）
# --------------------------------------------------------------------------- #
def test_C_merge_import_with_insertion_does_not_misplace() -> None:
    """在文件中间插入一行 → 其后 uid 全部漂移，旧译文不得错位。

    这是最容易「静默出错」的场景：插入后 ``matches`` 数量完全正常
    （旧代码 matches=5 unmatched=0），但 3 条译文写到了别的句子上。
    """
    f = "tl/chinese2/ch4.rpy"
    old = _make_segments(f, ["LINE1", "LINE2", "LINE3", "LINE4", "LINE5"])
    _assign(old)
    _apply_translations(old, {"LINE1": "译1", "LINE2": "译2",
                              "LINE3": "译3", "LINE4": "译4",
                              "LINE5": "译5"})
    old_rows = _as_rows(old)

    # 在第 2 段之后插入一行
    new = _make_segments(
        f, ["LINE1", "LINE2", "NEW INSERTED", "LINE3", "LINE4", "LINE5"])
    _assign(new)

    out = match_translations(new, old_rows)

    # 1) 绝不允许把译文写到原文不同的段上
    _assert_all_landed_correctly(new, out)

    # 2) 原有的 5 条译文都应当仍然找到正确归属
    landed = {(m.segment.source, m.row.target) for m in out.matches}
    for src, tgt in [("LINE1", "译1"), ("LINE2", "译2"), ("LINE3", "译3"),
                     ("LINE4", "译4"), ("LINE5", "译5")]:
        assert (src, tgt) in landed, f"{src} 的译文 {tgt} 丢失或错位"

    # 3) 新插入的行不得拿到任何旧译文
    for m in out.matches:
        if m.segment.source == "NEW INSERTED":
            pytest.fail(f"新插入的行被写入了旧译文 {m.row.target!r}")


def test_C_uid_really_does_drift_on_insertion() -> None:
    """证明场景 C 的前提成立：插入确实使 uid 后移（否则该用例无意义）。"""
    f = "tl/chinese2/ch4b.rpy"
    old = _make_segments(f, ["A", "B", "C", "D"])
    _assign(old)
    new = _make_segments(f, ["A", "B", "INSERTED", "C", "D"])
    _assign(new)

    old_map = {s.uid: s.source for s in old}
    drifted = [u for u, src in old_map.items()
               if u in {s.uid: s for s in new} and False]  # placeholder
    new_map = {s.uid: s.source for s in new}
    common = set(old_map) & set(new_map)
    drifted = [u for u in common if old_map[u] != new_map[u]]
    assert drifted, "前提不成立：插入后 uid 未漂移"
    # uid=make_uid(f,3) 原本是 C，现在是 INSERTED
    assert old_map[make_uid(f, 3)] == "C"
    assert new_map[make_uid(f, 3)] == "INSERTED"


# --------------------------------------------------------------------------- #
# D. 合并导入：删除一行（uid 整体前移）
# --------------------------------------------------------------------------- #
def test_D_merge_import_with_deletion_does_not_misplace() -> None:
    """删除一行 → 其后 uid 前移，旧译文不得错位。"""
    f = "tl/chinese2/ch5.rpy"
    old = _make_segments(f, ["LINE1", "LINE2", "LINE3", "LINE4", "LINE5"])
    _assign(old)
    _apply_translations(old, {"LINE1": "译1", "LINE2": "译2",
                              "LINE3": "译3", "LINE4": "译4",
                              "LINE5": "译5"})
    old_rows = _as_rows(old)

    new = _make_segments(f, ["LINE1", "LINE3", "LINE4", "LINE5"])
    _assign(new)

    out = match_translations(new, old_rows)
    _assert_all_landed_correctly(new, out)

    landed = {(m.segment.source, m.row.target) for m in out.matches}
    for src, tgt in [("LINE1", "译1"), ("LINE3", "译3"),
                     ("LINE4", "译4"), ("LINE5", "译5")]:
        assert (src, tgt) in landed, f"{src} 的译文丢失或错位"
    # 被删掉的 LINE2 译文应当 unmatched，而不是塞给别人
    assert any(r.source == "LINE2" for r in out.unmatched)


# --------------------------------------------------------------------------- #
# E. 合并导入：段落重排
# --------------------------------------------------------------------------- #
def test_E_merge_import_with_reorder_follows_source_not_index() -> None:
    """段落重排 → 译文必须跟着原文走，不能跟着 uid/序号走。"""
    f = "tl/chinese2/ch6.rpy"
    old = _make_segments(f, ["Alpha", "Beta", "Gamma", "Delta"])
    _assign(old)
    _apply_translations(old, {"Alpha": "甲", "Beta": "乙",
                              "Gamma": "丙", "Delta": "丁"})
    old_rows = _as_rows(old)

    new = _make_segments(f, ["Delta", "Beta", "Alpha", "Gamma"])
    _assign(new)

    out = match_translations(new, old_rows)
    _assert_all_landed_correctly(new, out)

    expect = {"Delta": "丁", "Beta": "乙", "Alpha": "甲", "Gamma": "丙"}
    landed = {m.segment.source: m.row.target for m in out.matches}
    assert landed == expect, f"重排后译文错位：{landed}"


# --------------------------------------------------------------------------- #
# F. 合并导入：原文被改写
# --------------------------------------------------------------------------- #
def test_F_merge_import_with_rewritten_line_does_not_reuse_old_target() -> None:
    """原文被改写成另一句话 → 不得把旧译文套上去。"""
    f = "tl/chinese2/ch7.rpy"
    old = _make_segments(f, ["Original sentence", "Another one"])
    _assign(old)
    _apply_translations(old, {"Original sentence": "旧译文",
                              "Another one": "另一条"})
    old_rows = _as_rows(old)

    new = _make_segments(
        f, ["Completely rewritten and much longer text here", "Another one"])
    _assign(new)

    out = match_translations(new, old_rows)
    # 被改写的那句不得拿到旧译文
    for m in out.matches:
        if m.segment.source.startswith("Completely rewritten"):
            pytest.fail(f"改写后的句子被套上了旧译文 {m.row.target!r}"
                        f"（方式={m.method}）")
    # 未改动的那句必须仍然正确
    ok = [m for m in out.matches if m.segment.source == "Another one"]
    assert ok and ok[0].row.target == "另一条"


# --------------------------------------------------------------------------- #
# G. 写回后重导入（regenerate → parse → merge）
# --------------------------------------------------------------------------- #
_RPY_TEMPLATE = """\
translate chinese2 ch_g_{tag}:

    # game/{file}:{line}
    old "{src}"
    new "{tgt}"
"""


def test_G_writeback_then_reimport_is_idempotent(tmp_path: Path) -> None:
    """写回 → 重新解析 → 再合并导入：译文必须原样保留，不得错位。

    这条覆盖用户描述的真实流程：翻译 → 写回 → 再次导入 → 再翻译。
    写回会改变行号，因此 locator 全变；uid 由序号推导，也必须仍然稳定。
    """
    from pytran.parser import RenpyParser

    f = "tl/chinese2/ch8.rpy"
    pairs = [("First line", "第一行"), ("Second line", "第二行"),
             ("Third line", "第三行")]

    # 构造一个「已写回」的 rpy
    body = "".join(
        _RPY_TEMPLATE.format(tag=i, file=f, line=10 + i * 3,
                             src=src, tgt=tgt)
        for i, (src, tgt) in enumerate(pairs, start=1)
    )
    rpy = tmp_path / "ch8.rpy"
    rpy.write_text(body, encoding="utf-8")

    parser = RenpyParser()
    segs1 = parser.parse_file(rpy, game_relpath=f)
    assert len(segs1) == 3, f"应解析出 3 段，实际 {len(segs1)}"
    _assign(segs1)
    for s in segs1:
        assert s.target and s.target.strip(), "已写回的文件应带译文"
        assert s.status == SegmentStatus.TRANSLATED

    # 再写回一次（regenerate）→ 再解析 → uid 必须与第一次一致
    project = build_project(segs1, reassign_uids=False)
    out_text = parser.regenerate(project, f)
    rpy2 = tmp_path / "ch8_again.rpy"
    rpy2.write_text(out_text, encoding="utf-8")
    segs2 = parser.parse_file(rpy2, game_relpath=f)
    _assign(segs2)

    assert [s.source for s in segs2] == [s.source for s in segs1]
    assert [s.uid for s in segs2] == [s.uid for s in segs1], (
        "写回后重导入 uid 漂移了——合并导入将错位")
    assert [s.target for s in segs2] == [s.target for s in segs1], (
        "写回后重导入译文变了")

    # 用第一轮的译文行去匹配第二轮，必须 100% 正确
    out = match_translations(segs2, _as_rows(segs1))
    assert len(out.matches) == 3
    _assert_all_landed_correctly(segs2, out)


def test_G_writeback_roundtrip_preserves_targets_exactly(tmp_path: Path) -> None:
    """regenerate 两次的结果必须逐字节相同（幂等）。"""
    from pytran.parser import RenpyParser

    f = "tl/chinese2/ch9.rpy"
    body = "".join(
        _RPY_TEMPLATE.format(tag=i, file=f, line=5 + i,
                             src=s, tgt=t)
        for i, (s, t) in enumerate(
            [("Apple", "苹果"), ("Banana", "香蕉")], start=1)
    )
    rpy = tmp_path / "ch9.rpy"
    rpy.write_text(body, encoding="utf-8")

    parser = RenpyParser()
    segs = parser.parse_file(rpy, game_relpath=f)
    _assign(segs)
    project = build_project(segs, reassign_uids=False)

    once = parser.regenerate(project, f)
    twice = parser.regenerate(project, f)
    assert once == twice, "regenerate 不幂等"


# --------------------------------------------------------------------------- #
# H. 多文件 strings 同特征码
# --------------------------------------------------------------------------- #
def test_H_strings_across_files_do_not_cross_match() -> None:
    """两个文件的 strings 块共用同一特征码，不得串译。

    真实事故（2026-10-25）：dlc.rpy 的 27 条译文被写进了
    exp-1-3somes.rpy 的 27 个 strings 段。
    """
    fa = "tl/chinese2/dlc.rpy"
    fb = "tl/chinese2/exp.rpy"

    segs_a = _make_segments(fa, ["Apple", "Banana", "Cherry"],
                            feature=STRINGS_FEATURE, kind="strings")
    _assign(segs_a)
    _apply_translations(segs_a, {"Apple": "苹果", "Banana": "香蕉",
                                 "Cherry": "樱桃"})

    # 另一个文件：同样 3 条 strings，但原文完全不同
    segs_b = _make_segments(fb, ["Dog", "Elephant", "Fox"],
                            feature=STRINGS_FEATURE, kind="strings")
    _assign(segs_b)

    rows = _as_rows(segs_a)
    out = match_translations(segs_b, rows)

    # 原文完全对不上 → 必须全部 unmatched，一条都不许落
    assert out.matches == [], (
        f"跨文件串译：{[(m.segment.source, m.row.target) for m in out.matches]}")
    assert len(out.unmatched) == 3


def test_H_strings_same_file_still_match_by_source() -> None:
    """同文件内 strings 仍能正确匹配（不能因噎废食）。"""
    f = "tl/chinese2/strings_ok.rpy"
    old = _make_segments(f, ["Apple", "Banana"], feature=STRINGS_FEATURE,
                         kind="strings")
    _assign(old)
    _apply_translations(old, {"Apple": "苹果", "Banana": "香蕉"})

    new = _make_segments(f, ["Apple", "Banana"], feature=STRINGS_FEATURE,
                         kind="strings")
    _assign(new)
    out = match_translations(new, _as_rows(old))

    assert len(out.matches) == 2
    _assert_all_landed_correctly(new, out)


# --------------------------------------------------------------------------- #
# I. path_hash 碰撞
# --------------------------------------------------------------------------- #
def test_I_path_hash_collision_does_not_cross_write() -> None:
    """两个文件 path_hash 碰撞（真实可复现）时不得跨文件覆盖。

    ``tl/chinese2/file_56.rpy`` 与 ``tl/chinese2/file_228.rpy`` 的
    path_hash 都是 40439，seq1 uid 都是 2650210305。
    """
    pa, pb = "tl/chinese2/file_56.rpy", "tl/chinese2/file_228.rpy"
    assert path_hash(pa) == path_hash(pb), "前提：这两路径确实碰撞"

    uid = make_uid(pa, 1)
    assert make_uid(pb, 1) == uid

    seg_a = _seg(uid, pa, f"# game/{pa}:1", "FIREBALL")
    seg_b = _seg(uid, pb, f"# game/{pb}:1", "ICE SHIELD")
    rows = [_row("FIREBALL", "火球术", file=pa, line=1, uid=uid)]

    # 无论工程里两文件的先后顺序如何，译文都必须落到 file_56
    for order in ([seg_a, seg_b], [seg_b, seg_a]):
        out = match_translations(order, rows)
        assert len(out.matches) == 1
        m = out.matches[0]
        assert m.segment.file == pa, (
            f"译文落到了 {m.segment.file}（应为 {pa}）")
        assert m.segment.source == "FIREBALL"


def test_I_hash_collision_rate_is_bounded() -> None:
    """历史事实存档：16-bit 路径 hash 的碰撞率（复盘 陷阱二/四 的物证）。

    b13ce9e 曾断言「不同文件 hash 不同永不撞车」，实测 3000 路径 59 次
    碰撞。v3.1 起身份不含 hash（完整路径入键），``path_hash`` 退化为
    常量 0——本用例改为固化"hash 方案已被证伪并移除"这一事实，防止
    后人重新引入。
    """
    from pytran.uids import path_hash
    paths = [f"tl/chinese2/file_{i}.rpy" for i in range(3000)]
    # v3.1：path_hash 已废弃为常量；身份键使用完整路径，构造上无碰撞
    from pytran.identity import segment_key
    keys = [segment_key(p, "same source") for p in paths]
    assert len(set(keys)) == 3000, "身份键必须按完整路径唯一"
    assert path_hash(paths[0]) == 0, "path_hash 应已退役为常量"


# --------------------------------------------------------------------------- #
# J. CSV 往返
# --------------------------------------------------------------------------- #
def test_J_csv_roundtrip_does_not_misplace(tmp_path: Path) -> None:
    """导出 CSV → 导入 CSV，译文必须回到原来那一段。"""
    from pytran.exporter import ExporterImpl
    from pytran.config import TermProfile

    f = "tl/chinese2/ch10.rpy"
    segs = _make_segments(f, ["Cat", "Dog", "Bird"])
    _assign(segs)
    _apply_translations(segs, {"Cat": "猫", "Dog": "狗", "Bird": "鸟"})
    project = build_project(segs, reassign_uids=False)

    exporter = ExporterImpl(parser=None)
    csv_path = exporter.export_csv(project, tmp_path)
    assert Path(csv_path).exists()

    # 清空译文，再导入
    for s in project.segments:
        s.target = None
        s.status = SegmentStatus.PENDING

    updated, unmatched = exporter.import_csv(
        project, csv_path, TermProfile())
    assert updated == 3, f"应更新 3 行，实际 {updated}"
    assert unmatched == 0

    got = {s.source: s.target for s in project.segments}
    assert got == {"Cat": "猫", "Dog": "狗", "Bird": "鸟"}, f"CSV 往返错位：{got}"


def test_J_csv_with_shifted_uid_falls_back_to_source(tmp_path: Path) -> None:
    """CSV 里的 uid 与当前工程不一致时，必须按原文兜底而非按位置硬塞。"""
    from pytran.exporter import ExporterImpl
    from pytran.config import TermProfile

    f = "tl/chinese2/ch11.rpy"
    segs = _make_segments(f, ["Cat", "Dog"])
    _assign(segs)
    _apply_translations(segs, {"Cat": "猫", "Dog": "狗"})
    project = build_project(segs, reassign_uids=False)

    exporter = ExporterImpl(parser=None)
    csv_path = exporter.export_csv(project, tmp_path)

    # 模拟文件被编辑：在前面插入一行 → uid 全部后移
    segs2 = _make_segments(f, ["INSERTED", "Cat", "Dog"])
    _assign(segs2)
    project2 = build_project(segs2, reassign_uids=False)

    exporter.import_csv(project2, csv_path, TermProfile())

    got = {s.source: s.target for s in project2.segments}
    assert got.get("Cat") == "猫", f"Cat 的译文错位：{got}"
    assert got.get("Dog") == "狗", f"Dog 的译文错位：{got}"
    assert got.get("INSERTED") is None, (
        f"新插入的行被写入了旧译文：{got}")


# --------------------------------------------------------------------------- #
# K. 熔断：错位必须被拦住
# --------------------------------------------------------------------------- #
def test_K_fuse_blocks_misplaced_target() -> None:
    """错位写入的译文（原文与译文语义/标签不符）必须被写回熔断拦住。"""
    f = "tl/chinese2/ch12.rpy"
    seg = _seg(make_uid(f, 1), f, f"# game/{f}:1",
               "Take me to the Sylphir Island")
    seg.target = "{image=it_qu.webp} 波泽拉的水晶球"
    seg.status = SegmentStatus.TRANSLATED

    project = build_project([seg], reassign_uids=False)
    fuse = evaluate_export_fuse(project)

    assert "tag_check" in fuse, f"错位译文未被熔断拦住：{fuse}"


def test_K_fuse_passes_clean_project() -> None:
    """正常工程必须放行（熔断不能误伤）。"""
    f = "tl/chinese2/ch13.rpy"
    seg = _seg(make_uid(f, 1), f, f"# game/{f}:1", "Hello")
    seg.target = "你好"
    seg.status = SegmentStatus.TRANSLATED

    project = build_project([seg], reassign_uids=False)
    assert evaluate_export_fuse(project) == {}
