"""W5c：术语候选抽取算法重做（docs/W5c-术语抽取重做.md §2/§5）。

覆盖四个信号源与合并规则，全部为**纯逻辑**测试（不依赖 PyQt6 / 网络 / 磁盘
全局状态；临时 rpy 目录用 ``tmp_path``）：

* 信号 A：``define x = Character("N")`` 各形态；``DynamicCharacter`` 不命中；
* 信号 D：``Segment.role`` 计数、引擎内置 role 剔除、无 ``game_root`` 仍工作；
* 信号 B：``kind == "strings"`` 段的 ``old "X"`` 短串阈值；
* 信号 C：AI mock 返回幻觉词 → 被 source 出现次数过滤；异常静默跳过；
* 合并：大小写不敏感去重、来源优先级、``limit`` 不挤压 A/D；
* 过滤：已在 ``terms`` / ``exempt_english`` 的条目被剔除；
* 边界：空工程 / 无 ``game_root`` / ``segments=None`` 不抛错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pytran.config import TermProfile
from pytran.models import Segment
from pytran.terms import (
    CHAR_DEF_RE,
    SOURCE_AI,
    SOURCE_CHARACTER,
    SOURCE_OLD_STRING,
    SOURCE_ROLE,
    Candidate,
    extract_candidates,
)


def _seg(uid: int, kind: str = "dialogue", role: str = "",
         source: str = "") -> Segment:
    return Segment(uid=uid, file="a.rpy", locator=f"l{uid}", feature="f",
                   kind=kind, comment="", role=role, source=source)


def _game_root(tmp_path: Path, name: str, lines: list[str]) -> Path:
    """构造一个最小 Ren'Py 游戏目录（``<tmp>/<name>/game/*.rpy``）。"""
    game = tmp_path / name / "game"
    game.mkdir(parents=True, exist_ok=True)
    (game / "script.rpy").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path / name


class _FakeAI:
    """信号 C 替身：返回固定文本（模拟模型输出）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete_text(self, prompt: str) -> str:
        self.calls += 1
        return self.text


class _BrokenAI:
    """信号 C 替身：总是抛错（验证静默降级）。"""

    def complete_text(self, prompt: str) -> str:
        raise RuntimeError("boom")


# --------------------------------------------------------------------------- #
# 信号 A：Ren'Py 角色定义
# --------------------------------------------------------------------------- #
def test_signal_a_character_define_forms(tmp_path: Path) -> None:
    """A：三种 define 形态全部命中，DynamicCharacter 不命中（§5 验收）。"""
    root = _game_root(tmp_path, "g1", [
        'define hor = Character("Horos")',
        'define lena = Character(_("Lena"))',
        "define dyn = DynamicCharacter('x')",
        "define n = Character(None, kind=adv)",
    ])
    cands = extract_candidates([], None, game_root=root)
    terms = {c.term for c in cands}
    assert "Horos" in terms, "最常见的无包装形态必须命中"
    assert "Lena" in terms, "_(\"N\") 可翻译包装形态必须命中"
    assert "x" not in terms, "DynamicCharacter 是动态名，不命中"
    assert all(c.source == SOURCE_CHARACTER for c in cands)


def test_char_def_re_matches_bare_and_wrapped() -> None:
    """A：正则本身必须同时吃下 `Character("N")` 与 `Character(_("N"))`。

    回归：任务书原稿的 `_?\\(` 把 `_` 与 `(` 各自设为可选，导致最常见的
    无包装形态匹配不到。
    """
    assert CHAR_DEF_RE.match('define hor = Character("Horos")')
    assert CHAR_DEF_RE.match('define lena = Character(_("Lena"))')
    assert CHAR_DEF_RE.match('  define h = Character(  "Spaced"  )')
    assert not CHAR_DEF_RE.match("define d = DynamicCharacter('x')")
    assert not CHAR_DEF_RE.match("define n = Character(None, kind=adv)")


def test_signal_a_empty_when_no_rpy_on_disk(tmp_path: Path) -> None:
    """A：目录里只有 rpyc（未解包）→ 信号 A 自然为空，不抛错。"""
    root = tmp_path / "packed"
    (root / "game").mkdir(parents=True)
    (root / "game" / "script.rpyc").write_bytes(b"RENPY RPC2\x00garbage")
    assert extract_candidates([], None, game_root=root) == []


# --------------------------------------------------------------------------- #
# 信号 D：Segment.role
# --------------------------------------------------------------------------- #
def test_signal_d_role_without_game_root() -> None:
    """D：无 game_root（纯文件模式）时 role 信号仍工作。"""
    segs = [
        _seg(1, role="Horos", source="Horos waves at Lena"),
        _seg(2, role="Lena", source="Lena waves back at Horos"),
    ]
    cands = extract_candidates(segs, None)
    assert {c.term for c in cands} == {"Horos", "Lena"}
    assert {c.source for c in cands} == {SOURCE_ROLE}


def test_signal_d_excludes_engine_roles() -> None:
    """D：Ren'Py 引擎内置 role（narrator/centered/…）是噪声，必须剔除。"""
    segs = [
        _seg(1, role="narrator", source="The sun rises over Horos."),
        _seg(2, role="centered", source="Chapter One"),
        _seg(3, role="vcentered", source="The End"),
        _seg(4, role="thought", source="Horos wondered."),
        _seg(5, role="Horos", source="Horos wondered."),
    ]
    terms = {c.term for c in extract_candidates(segs, None)}
    assert terms == {"Horos"}
    assert not ({"narrator", "centered", "vcentered", "thought"} & terms)


def test_signal_d_skips_sentence_like_roles() -> None:
    """D：超长 / 多词 / 纯数字 role 不是人名，应剔除。"""
    segs = [
        _seg(1, role="This is a whole sentence not a name", source="x"),
        _seg(2, role="12345", source="x"),
        _seg(3, role="", source="x"),
        _seg(4, role="Ian", source="x"),
    ]
    terms = {c.term for c in extract_candidates(segs, None)}
    assert terms == {"Ian"}


# --------------------------------------------------------------------------- #
# 信号 B：old "X" 短串
# --------------------------------------------------------------------------- #
def test_signal_b_old_string_thresholds() -> None:
    """B：`old "Ian"` 入选；`old "Click to continue"` 不入选（§5 验收）。"""
    segs = [
        _seg(1, kind="strings", source="Ian"),
        _seg(2, kind="strings", source="Stalkfap"),
        _seg(3, kind="strings", source="Click to continue"),
        _seg(4, kind="strings", source="This string is definitely longer than twenty four chars"),
        _seg(5, kind="strings", source="12345"),
        _seg(6, kind="strings", source=""),
    ]
    terms = {c.term for c in extract_candidates(segs, None)}
    assert "Ian" in terms
    assert "Stalkfap" in terms
    assert "Click to continue" not in terms, "含虚词 to → 是句子不是术语"
    assert not any(len(t) > 24 for t in terms), "超过 24 字符的整串不入选"
    assert "12345" not in terms, "纯数字不入选"


def test_signal_b_ignores_dialogue_kind() -> None:
    """B：只认 kind == "strings"；dialogue 段不参与信号 B。"""
    segs = [_seg(1, kind="dialogue", role="", source="Ian")]
    assert extract_candidates(segs, None) == []


# --------------------------------------------------------------------------- #
# 信号 C：AI 识别
# --------------------------------------------------------------------------- #
def test_signal_c_filters_hallucinated_names() -> None:
    """C：mock 返回 ["Horos","FakeName"]，FakeName 出现 0 次 → 被过滤（§5）。"""
    segs = [
        _seg(1, role="Horos", source="Horos speaks to Lena"),
        _seg(2, role="Lena", source="Lena answers Horos"),
    ]
    ai = _FakeAI("Horos\nFakeName\nLena")
    cands = extract_candidates(segs, None, ai_helper=ai)
    terms = {c.term for c in cands}
    assert "Horos" in terms
    assert "FakeName" not in terms, "工程中出现 0 次的词必须被过滤"
    assert ai.calls == 1, "一次抽取最多调用 AI 一次"


def test_signal_c_not_called_when_no_dialogue() -> None:
    """C：没有 dialogue 段可采样时不调 AI（省一次无效请求）。"""
    ai = _FakeAI("Horos")
    segs = [_seg(1, kind="strings", source="Ian")]
    cands = extract_candidates(segs, None, ai_helper=ai)
    assert ai.calls == 0, "无可采样对话时不得调用 AI"
    # 信号 B 仍正常工作
    assert [c.term for c in cands] == ["Ian"]
    assert cands[0].source == SOURCE_OLD_STRING


def test_signal_c_swallows_errors() -> None:
    """C：AI 抛错必须静默跳过，不成为抽取失败的原因（§6 红线）。"""
    segs = [_seg(1, role="Horos", source="Horos speaks")]
    cands = extract_candidates(segs, None, ai_helper=_BrokenAI())
    assert [c.term for c in cands] == ["Horos"], "结构信号应照常返回"


def test_signal_c_requires_two_occurrences() -> None:
    """C：出现 1 次的词不入选（防幻觉阈值 >= 2）。"""
    segs = [_seg(1, role="Horos", source="Horos mentions Lena once")]
    cands = extract_candidates(segs, None, ai_helper=_FakeAI("Horos\nLena"))
    assert {c.term for c in cands} == {"Horos"}
    # Horos 被 D 与 C 同时命中 → 全局合并后只出现一次，来源取更可靠的 D
    horos = [c for c in cands if c.term == "Horos"]
    assert len(horos) == 1
    assert horos[0].source == SOURCE_ROLE


# --------------------------------------------------------------------------- #
# 合并、排序、limit
# --------------------------------------------------------------------------- #
def test_merge_dedup_case_insensitive() -> None:
    """合并：大小写不敏感去重，保留首次出现的原始大小写（§7.6-1）。"""
    segs = [
        _seg(1, kind="strings", source="Horos"),
        _seg(2, kind="strings", source="horos"),
        _seg(3, kind="strings", source="HOROS"),
    ]
    cands = extract_candidates(segs, None)
    assert len(cands) == 1
    assert cands[0].term == "Horos"


def test_merge_source_priority_a_over_d(tmp_path: Path) -> None:
    """合并：A 与 D 撞名 → 来源标 A，count 取最大（§5 验收）。"""
    root = _game_root(tmp_path, "g2", ['define h = Character("Horos")'])
    segs = [
        _seg(1, role="Horos", source="Horos"),
        _seg(2, role="Horos", source="Horos"),
    ]
    cands = extract_candidates(segs, None, game_root=root)
    assert len(cands) == 1
    assert cands[0].term == "Horos"
    assert cands[0].source == SOURCE_CHARACTER, "撞名时取最可靠来源"
    assert cands[0].count == 2, "count 取各信号最大值，不因降级而变小"


def test_sort_order_a_d_b_c(tmp_path: Path) -> None:
    """排序：来源 A → D → B → C，同源按次数降序（§3）。"""
    root = _game_root(tmp_path, "g3", [
        'define a = Character("Aaa")',
        'define b = Character("Zzz")',
    ])
    segs = [
        _seg(1, role="Ddd", source="Ddd"),
        _seg(2, role="Ddd", source="Ddd"),
        _seg(3, kind="strings", source="Bbb"),
    ]
    cands = extract_candidates(segs, None, game_root=root)
    assert [c.source for c in cands] == [
        SOURCE_CHARACTER, SOURCE_CHARACTER, SOURCE_ROLE, SOURCE_OLD_STRING,
    ]
    # A 内部按 count 降序（两者都是 1）→ 字典序
    assert [c.term for c in cands[:2]] == ["Aaa", "Zzz"]
    assert cands[-1].term == "Bbb"


def test_limit_does_not_evict_authoritative() -> None:
    """limit 只约束 B/C，A/D 角色名全部展示（§7.6-3）。"""
    segs = [_seg(i, kind="strings", source=f"Name{i:03d}") for i in range(80)]
    segs.append(_seg(200, role="Horos", source="x"))
    segs.append(_seg(201, role="Lena", source="x"))
    cands = extract_candidates(segs, None, limit=50)
    assert len(cands) == 50
    authoritative = {c.term for c in cands
                     if c.source in (SOURCE_CHARACTER, SOURCE_ROLE)}
    assert authoritative == {"Horos", "Lena"}, "A/D 不应被 limit 挤掉"


def test_limit_zero_returns_empty() -> None:
    """limit=0 → 不返回任何候选（含 A/D）。"""
    segs = [_seg(1, role="Horos", source="x")]
    assert extract_candidates(segs, None, limit=0) == []


# --------------------------------------------------------------------------- #
# profile 过滤
# --------------------------------------------------------------------------- #
def test_existing_terms_and_exempt_are_filtered() -> None:
    """已在 terms / exempt_english 的条目（大小写不敏感）被剔除。"""
    profile = TermProfile(terms={"Lena": "莉娜"}, exempt_english=["Stalkfap"])
    segs = [
        _seg(1, kind="strings", source="Lena"),
        _seg(2, kind="strings", source="stalkfap"),   # 大小写不同也要剔除
        _seg(3, kind="strings", source="Ian"),
    ]
    terms = {c.term for c in extract_candidates(segs, profile)}
    assert terms == {"Ian"}


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #
def test_empty_inputs_return_empty_list() -> None:
    """空工程 / 无 game_root / segments=None → [] 不抛错（§5 验收）。"""
    assert extract_candidates([], None) == []
    assert extract_candidates([], None, game_root=None) == []
    assert extract_candidates(None, None) == []
    assert extract_candidates([], None, game_root=Path("/no/such/dir")) == []


def test_profile_none_is_allowed() -> None:
    """profile=None 时不查既有术语表，直接返回全部候选。"""
    segs = [_seg(1, role="Horos", source="x")]
    assert len(extract_candidates(segs, None)) == 1


def test_candidate_carries_source_field() -> None:
    """Candidate 需带 source（弹窗「来源」列依赖；W5c §3）。"""
    c = Candidate("Horos", 3, SOURCE_CHARACTER)
    assert c.term == "Horos" and c.count == 3 and c.source == SOURCE_CHARACTER
    assert c == Candidate("Horos", 3, SOURCE_CHARACTER)
    assert c != Candidate("Horos", 3, SOURCE_ROLE)


def test_candidate_source_defaults_to_role() -> None:
    """source 有默认值，旧构造方式（不含 source）不炸。"""
    assert Candidate("Horos", 1).source == SOURCE_ROLE


def test_ai_source_tag_used_when_only_ai_hits() -> None:
    """纯 C 命中的候选来源标 C（用户据此抽查）。"""
    segs = [_seg(1, role="narrator", source="Horos meets Lena twice"),
            _seg(2, role="narrator", source="Horos and Lena again")]
    cands = extract_candidates(segs, None, ai_helper=_FakeAI("Horos\nLena"))
    by_term = {c.term: c for c in cands}
    assert by_term["Horos"].source == SOURCE_AI
    assert by_term["Lena"].source == SOURCE_AI
    assert by_term["Horos"].count == 2, "C 的 count 是 source 中的真实出现次数"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
