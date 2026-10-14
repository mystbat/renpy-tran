"""术语表抽取与表格↔profile 的纯逻辑（W5b，设计 §3.2/§3.3/§4）。

本模块**不依赖 PyQt6**，可在无头环境直接 import 与测试；GUI 对话框
（`pytran.ui.PytranApp._open_settings`）只负责把控件数据喂给这里的纯函数，
再把结果写回 ``TermProfile``。这样试纸化测试（pytest）只覆盖逻辑，
真实窗口交互交由用户实测（§0.5 纪律）。

数据契约见 ``pytran.config.TermProfile``（T0 冻结，勿改）：
``terms: dict[str,str]`` / ``exempt_english: list[str]`` /
``prompt_overrides: dict[str,str]``（有效键 ``system_append``）。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Mapping, Sequence

from pytran.config import TermProfile
from pytran.models import Segment

__all__ = [
    "extract_candidates",
    "profile_to_rows",
    "rows_to_terms",
    "detect_duplicate_originals",
    "apply_terms",
    "Candidate",
]

# 一个「词」：拉丁/扩展字母开头的字母数字串（含 ' 与 -）。
_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’\-]*")

# 候选词组最长词数（§4：1~3 词连续大写开头）。
_MAX_SPAN = 3

# 入选阈值：dialogue 候选须出现 ≥3 次（§4「多段出现 ≥3 次入选」）。
_MIN_COUNT = 3


def _is_capitalized(token: str) -> bool:
    return bool(token) and token[:1].isupper()


def _has_english(text: str) -> bool:
    """原文是否含有英文（拉丁）字母——用于「空译名+英文→豁免」判定。"""
    return any(c.isascii() and c.isalpha() for c in (text or ""))


class Candidate:
    """一个抽取出的候选术语（§4 输出）。"""

    __slots__ = ("term", "count")

    def __init__(self, term: str, count: int) -> None:
        self.term = term
        self.count = count

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Candidate({self.term!r}, {self.count})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Candidate)
            and self.term == other.term
            and self.count == other.count
        )


def _strings_candidates(sources: Iterable[str]) -> Counter:
    """``kind="strings"`` 段：整条 source 多为角色名/短语，全条计入（§4）。"""
    counts: Counter = Counter()
    for src in sources:
        s = (src or "").strip()
        if s:
            counts[s] += 1
    return counts


def _dialogue_candidates(sources: Iterable[str]) -> Counter:
    """``kind="dialogue"`` 段：抽取 1~3 词连续大写开头专有名词候选。

    启发式排错（§4「排除句首普通词」）：一个候选词组**只在每段的开头位置
    （token 下标恒为 0）出现**时，视为普通句首词而剔除；只有跨段、或在段中
    部也出现过的才入选。同时须满足总出现次数 ≥ ``_MIN_COUNT``。
    """
    counts: Counter = Counter()
    # phrase -> 出现过的 token 下标集合（跨段合并，用于判断是否「仅句首」）
    positions: dict[str, set[int]] = {}
    for src in sources:
        toks = _WORD_RE.findall(src or "")
        n = len(toks)
        for i in range(n):
            if not _is_capitalized(toks[i]):
                continue
            for length in range(1, _MAX_SPAN + 1):
                if i + length > n:
                    break
                span = toks[i : i + length]
                if not all(_is_capitalized(t) for t in span):
                    break
                phrase = " ".join(span)
                counts[phrase] += 1
                positions.setdefault(phrase, set()).add(i)
    result: Counter = Counter()
    for phrase, c in counts.items():
        if c < _MIN_COUNT:
            continue
        # 仅在所有出现都位于段首（下标恒为 0）时剔除
        if positions.get(phrase) == {0}:
            continue
        result[phrase] = c
    return result


def extract_candidates(
    segments: Sequence[Segment],
    profile: TermProfile | None = None,
    limit: int = 50,
) -> list[Candidate]:
    """从工程全部段抽取候选术语（§4）。

    流程：字符串段整条计入 + 对白段抽大写词组 → 合并计数 → 过滤
    （已在 terms / 命中 exempt_english / 纯数字或单字母）→ 降序取前 ``limit``。
    """
    strings_srcs = (s.source for s in segments if getattr(s, "kind", "") == "strings")
    dialogue_srcs = (s.source for s in segments if getattr(s, "kind", "") == "dialogue")

    counts: Counter = Counter()
    counts.update(_strings_candidates(strings_srcs))
    counts.update(_dialogue_candidates(dialogue_srcs))

    existing_terms: Mapping[str, str] = (
        (profile.terms or {}) if profile is not None else {}
    )
    exempt: Iterable[str] = (
        (profile.exempt_english or []) if profile is not None else []
    )
    exempt_set = {str(x) for x in exempt}

    filtered: dict[str, int] = {}
    for term, c in counts.items():
        if term in existing_terms or term in exempt_set:
            continue
        if _is_pure_digit_or_single_letter(term):
            continue
        filtered[term] = c

    ordered = sorted(filtered.items(), key=lambda kv: (-kv[1], kv[0]))
    return [Candidate(term, c) for term, c in ordered[:limit]]


def _is_pure_digit_or_single_letter(term: str) -> bool:
    """纯数字 / 单字母候选跳过（§4）。多词词组不在此列。"""
    t = (term or "").strip()
    if not t:
        return True
    if len(t) == 1 and t.isalpha():
        return True
    if t.isdigit():
        return True
    return False


def profile_to_rows(profile: TermProfile) -> list[tuple[str, str]]:
    """profile.terms → 按原文排序的 (原文, 译名) 行，供表格显示（§3.2）。"""
    terms = profile.terms or {}
    return sorted(((str(k), str(v)) for k, v in terms.items()), key=lambda kv: kv[0])


def rows_to_terms(
    rows: Sequence[tuple[str, str]],
) -> tuple[dict[str, str], list[str]]:
    """表格行 → (terms, auto_exempt)。

    - 空原文行忽略；
    - 原文重复时**后者覆盖前者**（合并语义，§3.2）；
    - 译名为空且原文含英文 → 进 ``auto_exempt``（"按原文保留"语义，§3.2 保存）。
    """
    terms: dict[str, str] = {}
    auto_exempt: list[str] = []
    for original, translation in rows:
        o = (original or "").strip()
        if not o:
            continue
        t = (translation or "").strip()
        if not t and _has_english(o):
            auto_exempt.append(o)
        terms[o] = t
    return terms, auto_exempt


def detect_duplicate_originals(rows: Sequence[tuple[str, str]]) -> list[str]:
    """返回重复出现的原文（用于保存时提示「已合并」，§3.2）。"""
    seen: set[str] = set()
    dups: set[str] = set()
    for original, _ in rows:
        o = (original or "").strip()
        if not o:
            continue
        if o in seen:
            dups.add(o)
        else:
            seen.add(o)
    return sorted(dups)


def apply_terms(
    profile: TermProfile,
    rows: Sequence[tuple[str, str]],
    exempt_enabled: bool = False,
    exempt_lines: Sequence[str] | None = None,
    system_append: str | None = None,
) -> TermProfile:
    """把表格/豁免/规则页的编辑合并回 ``profile``，返回（原地修改并）同一实例。

    - ``terms`` 来自表格行（含空译名英文原文 → 自动进豁免）；
    - ``exempt_english`` = 手动豁免行（仅当 ``exempt_enabled``）+ 自动豁免去重；
    - ``prompt_overrides["system_append"]`` 仅在 ``system_append is not None`` 时更新。
    """
    terms, auto_exempt = rows_to_terms(rows)

    manual: list[str] = []
    if exempt_enabled:
        manual = [str(x).strip() for x in (exempt_lines or []) if str(x).strip()]

    # 去重且保序：手动在前、自动在后
    exempt_english: list[str] = []
    seen_ex: set[str] = set()
    for x in manual + auto_exempt:
        if x not in seen_ex:
            seen_ex.add(x)
            exempt_english.append(x)

    profile.terms = terms
    profile.exempt_english = exempt_english
    if system_append is not None:
        if not profile.prompt_overrides:
            profile.prompt_overrides = {}
        profile.prompt_overrides["system_append"] = system_append
    return profile
