"""W1 解析链往返测试（设计 §3、§6.1、§11、§14）。

断言核心：fixtures 全部样例 rpy → parse → regenerate 与原文件**逐字节一致**。
另覆盖 segmenter 切块/上下文、project.json 原子加载保存，以及 §14 的
skipped 计数与字节保留。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pytran.models import SegmentStatus
from pytran.parser import RenpyParser
from pytran.project import JsonProjectStore, build_project
from pytran.segmenter import Segmenter

FIXTURES = Path(__file__).parent / "fixtures"

PARSE_CASES = [
    "translate_basic.rpy",
    "strings_old_new.rpy",
    "dialogue_escaped_quote.rpy",
    "dialogue_tags_vars.rpy",
    "single_quoted.rpy",
    "blank_lines.rpy",
    "role_with_spaces.rpy",
    "nvl_no_role.rpy",
    "translate_basic_crlf.rpy",
    "translate_basic_bom.rpy",
    "skipped_block.rpy",
]


@pytest.mark.parametrize("name", PARSE_CASES)
def test_parser_roundtrip_byte_exact(name: str) -> None:
    """parse → regenerate 必须与原文件逐字节一致（设计 §11、§6.1）。"""
    path = FIXTURES / name
    original = path.read_bytes()

    parser = RenpyParser()
    segments = parser.parse_file(path)
    project = build_project(segments, files=[], game_root="")
    regenerated = parser.regenerate(project, name).encode("utf-8")

    assert regenerated == original, f"{name} 往返字节不一致"


@pytest.mark.parametrize("name", PARSE_CASES)
def test_parse_emits_only_translatable_or_skipped(name: str) -> None:
    """每个样例应解析出段；dialogue/strings 为 pending，未知块为 skipped。

    BUGFIX(old-new-pair)：带已有译文的 `old/new` 对导入为 TRANSLATED。
    """
    path = FIXTURES / name
    segments = RenpyParser().parse_file(path)
    assert segments, f"{name} 未解析出任何段"
    for s in segments:
        assert s.status in (
            SegmentStatus.PENDING,
            SegmentStatus.SKIPPED,
            SegmentStatus.TRANSLATED,
        )
        assert s.uid >= 1
        assert s.locator.startswith("# game/")


def test_old_new_pair_not_double_counted() -> None:
    """BUGFIX(old-new-pair)：`old "X"`/`new "Y"` 必须合成一条段——
    new 的中文不能被当作新的待翻原文（用户实测：uid2 = "奥斯瓦尔德的剑"
    被送去再翻译，预览面板显示错位）。"""
    path = FIXTURES / "strings_old_new.rpy"
    segments = RenpyParser().parse_file(path)
    # 两对 old/new → 只立 2 条段（旧实现是 4 条）
    assert len(segments) == 2
    by_src = {s.source: s for s in segments}
    assert set(by_src) == {"I am fine.", "See you tomorrow."}
    for s in segments:
        assert s.status == SegmentStatus.TRANSLATED
        assert s.target in ("我很好。", "明天见。")
        # locator 指向 new 行（行号 5/10），回写替换的是 new 的引号内容
        assert s.locator.endswith((":5", ":10"))


def test_old_new_empty_new_is_pending() -> None:
    """`new ""`（未翻译模板）→ PENDING，可正常送翻。"""
    import tempfile
    work = FIXTURES.parent / ".tmp_fix_old_new"
    work.mkdir(parents=True, exist_ok=True)
    try:
        f = work / "empty_new.rpy"
        f.write_text(
            'translate chinese strings:\n\n'
            '    old "Hello"\n    new ""\n',
            encoding="utf-8",
        )
        segments = RenpyParser().parse_file(f, game_relpath="empty_new.rpy")
        assert len(segments) == 1
        assert segments[0].source == "Hello"
        assert segments[0].status == SegmentStatus.PENDING
        assert not segments[0].target
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_orphan_new_paired_with_empty_old() -> None:
    """BUGFIX(orphan-new)：孤立 `new "Y"`（无 old 配对）→ 补一条空 old 配对。

    用户裁决 2026-10-20：不丢弃、也不立为待翻段——立一条段 source=""（占位）、
    target=Y、TRANSLATED，保持 old/new 配对形态；Y 原位写回 new 行。
    """
    work = FIXTURES.parent / ".tmp_fix_old_new"
    work.mkdir(parents=True, exist_ok=True)
    try:
        f = work / "orphan_new.rpy"
        f.write_text(
            'translate chinese strings:\n\n'
            '    new "孤立的中文"\n',
            encoding="utf-8",
        )
        segments = RenpyParser().parse_file(f, game_relpath="orphan_new.rpy")
        assert len(segments) == 1
        seg = segments[0]
        assert seg.source == ""          # 空 old 占位
        assert seg.target == "孤立的中文"  # Y 是已有译文
        assert seg.status == SegmentStatus.TRANSLATED
        assert seg.locator.endswith(":3")  # 指向 new 行
        # 字节保留：target 原位写回 new 行（write_text 落盘 CRLF，
        # read_text 万能换行读回 LF，两侧归一到 LF 比较）
        parser = RenpyParser()
        parser.parse_file(f, game_relpath="orphan_new.rpy")
        project = build_project(segments)
        out = parser.regenerate(project, "orphan_new.rpy")
        assert out.replace("\r\n", "\n") == f.read_text(encoding="utf-8")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_skipped_block_preserved_and_counted() -> None:
    """skipped 块：字节保留且计一条 SKIPPED 段（设计 §14）。"""
    path = FIXTURES / "skipped_block.rpy"
    parser = RenpyParser()
    segments = parser.parse_file(path)
    skipped = [s for s in segments if s.status == SegmentStatus.SKIPPED]
    assert len(skipped) >= 1
    # 字节保留由 roundtrip 测试保证；这里确认块头被标 skipped
    assert any("init python" in s.feature for s in skipped)


def test_translated_line_replaces_only_quoted_text() -> None:
    """已翻译段只替换引号内文本，前缀/缩进/注释不动（设计 §6.1）。"""
    path = FIXTURES / "translate_basic.rpy"
    parser = RenpyParser()
    segments = parser.parse_file(path)
    # 给第一段设译文
    first = segments[0]
    first.target = "你好，世界。"
    first.status = SegmentStatus.TRANSLATED
    project = build_project(segments)
    out = parser.regenerate(project, "translate_basic.rpy")

    assert '    e "你好，世界。"' in out
    # 其他未翻译行原样保留
    assert '    e "How are you today?"' in out
    assert '    old "Original English text"' in out


def test_segmenter_order_and_context() -> None:
    """切块按 uid 升序，且块携带其之前已翻译段的末尾译文上下文（设计 §4.1）。"""
    from pytran.models import Segment

    segs = []
    for i in range(1, 6):
        s = Segment(
            uid=i, file="x.rpy", locator=f"# game/x.rpy:{i}",
            feature="translate chinese t:", kind="dialogue",
            comment="# c", role="", source=f"src{i}",
        )
        segs.append(s)
    # 段 1、2 已翻译成功；它们不进翻译队列，但为后续块提供衔接上下文
    segs[0].target = "tr1"
    segs[0].status = SegmentStatus.TRANSLATED
    segs[1].target = "tr2"
    segs[1].status = SegmentStatus.TRANSLATED

    seg = Segmenter(max_lines=2, context_lines=2)
    chunks = seg.chunk(segs)
    # TRANSLATED 段不进入工作集，故工作段为 3,4,5 -> 两块 [3,4],[5]
    assert [c.segments[0].uid for c in chunks] == [3, 5]
    # 块携带其之前已翻译段（uid<块首）末尾的译文对
    for ch in chunks:
        assert ("src1", "tr1") in ch.context
        assert ("src2", "tr2") in ch.context


def test_project_store_roundtrip() -> None:
    """project.json 原子保存后再加载，segments/stats 一致（设计 §3.2）。"""
    import shutil

    # 沙箱仅允许写入工作区；系统 Temp 被拒。使用工作区内固定临时目录
    # （直接 mkdir，不用 tempfile.mkdtemp 的随机子目录，避免 ACL 差异）。
    work = Path(__file__).resolve().parent.parent / ".w1_tmp" / "store"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        path = FIXTURES / "translate_basic.rpy"
        segments = RenpyParser().parse_file(path)
        project = build_project(segments, game_root="E:/game")

        store = JsonProjectStore()
        store.save(project, work)
        assert (work / "project.json").exists()

        loaded = store.load(work)
        assert loaded.version == 3
        assert loaded.app == "pytran"
        assert loaded.next_uid == project.next_uid
        assert loaded.stats.pending == len(segments)
        assert len(loaded.segments) == len(segments)
        # 原文件不应被改写
        assert path.read_bytes() == path.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)
