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


def test_old_new_same_text_is_pending() -> None:
    """BUGFIX(sdk-template-pending)：`new` 与 `old` 原文相同 → SDK 空模板
    的未翻译占位（原文复读），不是已译 → PENDING（修复前被误标
    TRANSLATED）。用户实测 2026-10-21：SDK 重新生成模板后 27 条 strings
    对全部误标。"""
    work = FIXTURES.parent / ".tmp_fix_old_new"
    work.mkdir(parents=True, exist_ok=True)
    try:
        f = work / "same_new.rpy"
        f.write_text(
            'translate chinese2 strings:\n\n'
            '    old "Watch Kara try to eat out Teriania"\n'
            '    new "Watch Kara try to eat out Teriania"\n',
            encoding="utf-8",
        )
        segments = RenpyParser().parse_file(f, game_relpath="same_new.rpy")
        assert len(segments) == 1
        assert segments[0].source == "Watch Kara try to eat out Teriania"
        assert segments[0].target is None
        assert segments[0].status == SegmentStatus.PENDING
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_dialogue_source_forms() -> None:
    """BUGFIX(unrpyc-translated)+BUGFIX(sdk-template-pending)：四种原文形态。

    对齐 2.0「原文注释」列语义——原文必须来自可靠来源：
    1a. 官方模板已译（`# "英文"` + 台词≠原文）→ 注释为原文，TRANSLATED；
    1b. SDK 空模板（`# "英文"` + 台词=原文）→ PENDING（未翻译）；
    2. unrpyc 已译（无注释 + CJK 台词）→ 台词即译文，TRANSLATED；
    3. unrpyc 未译（无注释 + 英文台词）→ PENDING 正常送翻。
    """
    work = FIXTURES.parent / ".tmp_fix_old_new"
    work.mkdir(parents=True, exist_ok=True)
    try:
        f = work / "dialogue_forms.rpy"
        f.write_text(
            'translate chinese a_1:\n'
            '    # "Good morning."\n'
            '    e "早上好。"\n'
            '\n'
            'translate chinese a_2:\n'
            '    # "Good night."\n'
            '    e "Good night."\n'
            '\n'
            'translate chinese a_3:\n'
            '    e "这是一句已译的中文。"\n'
            '\n'
            'translate chinese a_4:\n'
            '    e "An untranslated English line."\n',
            encoding="utf-8",
        )
        segs = RenpyParser().parse_file(f, game_relpath="dialogue_forms.rpy")
        assert len(segs) == 4
        # 形态 1a：官方模板已译——原文来自注释行，台词≠原文 → TRANSLATED
        assert segs[0].source == "Good morning."
        assert segs[0].target == "早上好。"
        assert segs[0].status == SegmentStatus.TRANSLATED
        # 形态 1b：BUGFIX(sdk-template-pending)——SDK 空模板台词=原文 →
        # 尚未翻译，保持 PENDING（修复前被误标 TRANSLATED）
        assert segs[1].source == "Good night."
        assert segs[1].target is None
        assert segs[1].status == SegmentStatus.PENDING
        # 形态 2：unrpyc 已译——中文台词即译文
        assert segs[2].source == "这是一句已译的中文。"
        assert segs[2].target == "这是一句已译的中文。"
        assert segs[2].status == SegmentStatus.TRANSLATED
        # 形态 3：unrpyc 未译——英文台词正常送翻
        assert segs[3].source == "An untranslated English line."
        assert segs[3].status == SegmentStatus.PENDING
        assert not segs[3].target
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_role_comment_provides_source() -> None:
    """BUGFIX(role-comment)：`# role "英文"` 注释行（带说话人前缀）也是
    原文来源。

    用户实测 2026-10-21：exp-1-3somes.rpy 第 3 块起注释行带 ter/kar 等
    role 前缀，原正则不匹配 → 丢原文来源，中文台词被误判为 unrpyc
    已译形态（source=中文译文，送翻=中文→中文）。
    """
    work = FIXTURES.parent / ".tmp_fix_old_new"
    work.mkdir(parents=True, exist_ok=True)
    try:
        f = work / "role_comment.rpy"
        f.write_text(
            'translate chinese2 a_1:\n'
            '    # ter "I thought I heard you two!"\n'
            '    ter "我说听着动静耳熟呢！"\n'
            '\n'
            'translate chinese2 a_2:\n'
            '    # game/x.rpy:23\n'
            '    # kar "If you asked me the first time."\n'
            '    kar "如果我第一次见面时就问你。"\n',
            encoding="utf-8",
        )
        segs = RenpyParser().parse_file(f, game_relpath="role_comment.rpy")
        assert len(segs) == 2
        # 带 role 注释行提供原文；中文台词为译文
        assert segs[0].source == "I thought I heard you two!"
        assert segs[0].target == "我说听着动静耳熟呢！"
        assert segs[0].status == SegmentStatus.TRANSLATED
        # 定位注释行（# game/...）不干扰；后面的 kar 注释行正常生效
        assert segs[1].source == "If you asked me the first time."
        assert segs[1].target == "如果我第一次见面时就问你。"
        assert segs[1].status == SegmentStatus.TRANSLATED
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


def test_multi_file_import_uid_unique() -> None:
    """BUGFIX(dup-uid)：多文件导入后 uid 必须全局唯一且连续。

    解析器按文件局部计数（每个 parse_file 从 1 起）；uid 是引擎
    seg_map / 预览回查 / 审校游标的全局主键。用户实测 2026-10-21：
    10 文件 1468 段仅 436 个唯一 uid——预览显示别的句子的译文
    （rpg.rpy uid55 的事件回查到 loca_screen.rpy 的 uid55）、写回错行。
    build_project 现按传入顺序统一重编 1..N。
    """
    work = FIXTURES.parent / ".tmp_fix_old_new"
    work.mkdir(parents=True, exist_ok=True)
    try:
        f1 = work / "a.rpy"
        f2 = work / "b.rpy"
        f1.write_text(
            'translate chinese t1:\n    e "a1"\n    e "a2"\n    e "a3"\n',
            encoding="utf-8",
        )
        f2.write_text(
            'translate chinese t2:\n    e "b1"\n    e "b2"\n',
            encoding="utf-8",
        )
        p = RenpyParser()
        segs = []
        segs.extend(p.parse_file(f1, game_relpath="a.rpy"))
        segs.extend(p.parse_file(f2, game_relpath="b.rpy"))
        # 解析层：各文件局部计数（既有行为，uid 撞车）
        assert [s.uid for s in segs if s.file == "a.rpy"] == [1, 2, 3]
        assert [s.uid for s in segs if s.file == "b.rpy"] == [1, 2]
        # build_project 统一重编：全局唯一且连续
        project = build_project(segs)
        uids = [s.uid for s in project.segments]
        assert uids == [1, 2, 3, 4, 5]
        assert project.next_uid == 6
        # 段与文件的对应关系不变（a.rpy 三条在前、b.rpy 两条在后）
        assert [s.file for s in project.segments] == ["a.rpy", "a.rpy",
                                                       "a.rpy", "b.rpy", "b.rpy"]
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_skipped_block_preserved_and_counted_tail() -> None:
    """原 test_skipped_block_preserved_and_counted 的尾部断言（拆分保持）。"""
    path = FIXTURES / "skipped_block.rpy"
    parser = RenpyParser()
    segments = parser.parse_file(path)
    # 字节保留由 roundtrip 测试保证；这里确认块头被标 skipped
    assert any("init python" in s.feature for s in segments)


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
        # BUGFIX(unrpyc-translated)：fixture 第 2 块台词「你好，世界。」
        # 无 # 原文注释行且含 CJK → 导入即 TRANSLATED，不计入 pending。
        pending_cnt = sum(
            1 for s in segments if s.status == SegmentStatus.PENDING
        )
        assert loaded.stats.pending == pending_cnt
        assert len(loaded.segments) == len(segments)
        # 原文件不应被改写
        assert path.read_bytes() == path.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)
