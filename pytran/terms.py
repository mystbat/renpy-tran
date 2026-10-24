"""术语表抽取与表格↔profile 的纯逻辑（W5b，设计 §3.2/§3.3/§4）。

本模块**不依赖 PyQt6**，可在无头环境直接 import 与测试；GUI 对话框
（`pytran.ui.PytranApp._open_settings`）只负责把控件数据喂给这里的纯函数，
再把结果写回 ``TermProfile``。这样试纸化测试（pytest）只覆盖逻辑，
真实窗口交互交由用户实测（§0.5 纪律）。

数据契约见 ``pytran.config.TermProfile``（T0 冻结，勿改）：
``terms: dict[str,str]`` / ``exempt_english: list[str]`` /
``prompt_overrides: dict[str,str]``（有效键 ``system_append``）。

W5c 重做（``docs/W5c-术语抽取重做.md``）：候选抽取不再做自然语言句子上的
1~3 词大写滑窗统计（产出的全是句首词与碎片），改为四信号源按可靠性排序——
A ``Character`` 定义 / D ``Segment.role`` / B ``old`` 短串 / C AI 识别。
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from pytran.config import TermProfile
from pytran.models import Segment

__all__ = [
    "extract_candidates",
    "profile_to_rows",
    "rows_to_terms",
    "detect_duplicate_originals",
    "apply_terms",
    "glossary_path",
    "write_glossary_csv",
    "write_glossary_csv_rows",
    "read_glossary_csv",
    "merge_glossary_into_profile",
    "Candidate",
    "SOURCE_CHARACTER",
    "SOURCE_ROLE",
    "SOURCE_OLD_STRING",
    "SOURCE_AI",
    "CHAR_DEF_RE",
]

# ---------------------------------------------------------------------------
# W5c 重做：权威定义优先，模型辅助（docs/W5c-术语抽取重做.md §2/§7）
#
# 旧实现在自然语言句子上做 1~3 词大写滑窗统计，产出的全是句首词与碎片；
# 新版改用四个信号源，按可靠性排序：
#   A  define x = Character("Name")   —— 权威定义，扫 game/**/*.rpy
#   D  Segment.role                  —— 解析器已拆出的说话人名（结构信号）
#   B  old "X" 短串（kind="strings"）—— 作者显式声明的可翻译单元
#   C  AI 识别                       —— 需在 source 中实际出现 >=2 次
# ---------------------------------------------------------------------------

# 信号源标记（候选弹窗「来源」列展示用）。
SOURCE_CHARACTER = "A"   # define x = Character("...")
SOURCE_ROLE = "D"        # Segment.role（对话行前缀的说话人名）
SOURCE_OLD_STRING = "B"  # old "X" 短串（kind == "strings"）
SOURCE_AI = "C"          # AI 识别（受出现次数校验）

# 一个「词」：拉丁/扩展字母开头的字母数字串（含 ' 与 -）。
# 仅用于信号 C 的 source 词频索引（校验 AI 返回词是否真的出现过）。
_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’\-]*")

# 来源优先级：A > D > B > C（W5c §7.6）。撞名时取最可靠来源。
_SOURCE_RANK = {
    SOURCE_CHARACTER: 0,
    SOURCE_ROLE: 1,
    SOURCE_OLD_STRING: 2,
    SOURCE_AI: 3,
}

# 信号 A：Ren'Py 角色定义（W5c §2 信号源 A）。
# 覆盖 `define x = Character("N")`（最常见）与 `define x = Character(_("N"))`；
# DynamicCharacter 不匹配（动态名，宁缺勿滥）。
# 注意：任务书原稿的 `_?\(` 把最常见的无包装形态漏掉了——`_` 与 `(` 必须
# 作为**一组**整体可选，否则 `Character("Horos")` 匹配不到。
CHAR_DEF_RE = re.compile(
    r'^\s*define\s+\w+\s*=\s*Character\(\s*(?:_\(\s*)?"(?P<name>[^"]+)"',
)

# 信号 D：role 入选下限。role 是结构信号，出现 1 次即可信；
# 仅要求它看起来像个名字而不是句子。
_ROLE_MIN_LEN = 2
_ROLE_MAX_LEN = 24
_ROLE_MAX_WORDS = 4

# 信号 B：`old "X"` 短串阈值（W5c §2 信号源 B + §7.3 收紧）。
_OLD_STRING_MAX_LEN = 24
_OLD_STRING_MAX_WORDS = 3

# 信号 C：AI 返回词必须在 source 中实际出现的次数（防幻觉）。
_AI_MIN_OCCURRENCES = 2
# 送模型的采样行数（W5c §2：随机采样 200 行）。
_AI_SAMPLE_LINES = 200
# 送模型的单行长度上限（避免把整篇原文灌进去）。
_AI_SAMPLE_MAX_CHARS = 6000

# Ren'Py 引擎内置 role 名（00definitions.rpy L436-441 / 00nvl_mode.rpy L436-445）。
# 解析器会把它们原样收进 Segment.role，属纯噪声，必须剔除。
_ENGINE_ROLES = frozenset({
    "narrator", "centered", "vcentered", "thought", "nvl", "nvl_narrator",
    "adv", "name_only", "say", "say-centered",
})

# 常见非术语虚词：role/old 短串里偶尔出现的功能词，单独成词时无意义。
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on",
    "at", "by", "for", "with", "from", "as", "is", "are", "was", "were",
    "be", "been", "it", "its", "this", "that", "these", "those", "you",
    "your", "we", "our", "they", "their", "he", "she", "his", "her",
    "not", "no", "yes", "so", "then", "than", "there", "here", "when",
    "what", "who", "how", "all", "any", "can", "will", "would", "may",
})


def _has_english(text: str) -> bool:
    """原文是否含有英文（拉丁）字母——用于「空译名+英文→豁免」判定。"""
    return any(c.isascii() and c.isalpha() for c in (text or ""))


class Candidate:
    """一个抽取出的候选术语（W5c §3 输出）。"""

    __slots__ = ("term", "count", "source")

    def __init__(self, term: str, count: int, source: str = SOURCE_ROLE) -> None:
        self.term = term
        self.count = count
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Candidate({self.term!r}, {self.count}, {self.source!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Candidate)
            and self.term == other.term
            and self.count == other.count
            and self.source == other.source
        )

    def __hash__(self) -> int:  # noqa: D105 - 与 __eq__ 配套
        return hash((self.term, self.count, self.source))


# ---------------------------------------------------------------------------
# 信号源 A：Ren'Py 角色定义
# ---------------------------------------------------------------------------

def _iter_script_dirs(game_root: str | Path | None, language: str = "") -> list[Path]:
    """定位可扫的 rpy 源码目录（W5c §7.2 降级链）。

    顺序：`game/`（原始脚本，含反编译产物）→ `game/tl/<language>`（tl 里的
    `old "X"`）。反编译默认落在 rpyc 同目录（`unren.decompile` 的
    `out_dir=rpyc.parent`），所以 `game/` 下通常已有解出的 `.rpy`。
    解包前（只有 rpyc）两个目录都不含可扫文本，信号 A 自然为空——
    这是预期行为，不是缺陷。
    """
    if not game_root:
        return []
    root = Path(game_root)
    if not root.is_dir():
        return []
    dirs: list[Path] = []
    game = root / "game"
    if game.is_dir():
        dirs.append(game)
        if language:
            tl = game / "tl" / language
            if tl.is_dir():
                dirs.append(tl)
    return dirs


def _character_candidates(
    game_root: str | Path | None,
    language: str = "",
) -> Counter:
    """信号 A：扫 `game/**/*.rpy` 里的 `define x = Character("Name")`。

    只认正则明确命中的形态；`DynamicCharacter` 不匹配（动态名，宁缺勿滥）。
    """
    counts: Counter = Counter()
    for directory in _iter_script_dirs(game_root, language):
        # rglob 覆盖子目录；tl 与 game 可能重复命中同名文件，Counter 自然累加。
        for rpy in sorted(directory.rglob("*.rpy")):
            try:
                text = rpy.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:  # pragma: no cover - 权限/占用
                continue
            for line in text.splitlines():
                m = CHAR_DEF_RE.match(line)
                if m is None:
                    continue
                name = (m.group("name") or "").strip()
                if _is_valid_term(name):
                    counts[name] += 1
    return counts


# ---------------------------------------------------------------------------
# 信号源 D：Segment.role
# ---------------------------------------------------------------------------

def _role_candidates(segments: Sequence[Segment]) -> Counter:
    """信号 D：`Segment.role` 就是说话人名（parser `_extract_dialogue` 已拆出）。

    结构信号，非统计启发式：出现 1 次即可信。只要求它「看起来像名字」——
    2~24 字符、≤4 词、非引擎内置 role、非纯虚词。
    """
    counts: Counter = Counter()
    for seg in segments:
        role = (getattr(seg, "role", "") or "").strip()
        if not _is_valid_term(role):
            continue
        if len(role) > _ROLE_MAX_LEN:
            continue
        if len(role.split()) > _ROLE_MAX_WORDS:
            continue
        if role.lower() in _ENGINE_ROLES:
            continue
        counts[role] += 1
    return counts


# ---------------------------------------------------------------------------
# 信号源 B：old "X" 短串
# ---------------------------------------------------------------------------

def _old_string_candidates(segments: Sequence[Segment]) -> Counter:
    """信号 B：`kind == "strings"` 段（源码即作者声明的 `old "X"` 短串）。

    任务书原方案是自行扫 rpy 找 `old`，但 `translate` 块内的 `old` 是上一轮
    翻译残留（parser L251，多为整句），只有 `strings` 块内的才是术语单元
    （parser L298）。直接读 segments 不碰磁盘、不重复解析逻辑。
    """
    counts: Counter = Counter()
    for seg in segments:
        if getattr(seg, "kind", "") != "strings":
            continue
        s = (getattr(seg, "source", "") or "").strip()
        if not _is_old_string_ok(s):
            continue
        counts[s] += 1
    return counts


def _is_old_string_ok(text: str) -> bool:
    """`old "X"` 短串入选条件（W5c §2 信号源 B + §7.3 收紧）。"""
    s = (text or "").strip()
    if not s or len(s) > _OLD_STRING_MAX_LEN:
        return False
    if not _has_english(s):
        return False
    words = s.split()
    if len(words) > _OLD_STRING_MAX_WORDS:
        return False
    # 至少一个词首字母大写：真专名几乎必满足，排除全小写 UI 文案
    if not any(w[:1].isupper() for w in words if w):
        return False
    # 含虚词 → 是句子而非专名（"Click to continue" / "Are you sure?"）
    return not any(w.lower() in _STOPWORDS for w in words)


# ---------------------------------------------------------------------------
# 信号源 C：AI 识别
# ---------------------------------------------------------------------------

def _sample_sources(segments: Sequence[Segment], max_lines: int) -> str:
    """采样对话原文供信号 C 使用（W5c §2：前 N 段 / 随机 200 行）。

    只取 dialogue 段：strings 段是短串、source 里没有上下文可判断专名。
    按 uid 排序后等距抽样，保证覆盖整个工程而不只取开头。
    """
    sources = [
        (getattr(s, "source", "") or "")
        for s in segments
        if getattr(s, "kind", "") == "dialogue" and (getattr(s, "source", "") or "").strip()
    ]
    if not sources:
        return ""
    if len(sources) > max_lines:
        step = len(sources) / max_lines
        sources = [sources[int(i * step)] for i in range(max_lines)]
    text = "\n".join(sources)
    return text[:_AI_SAMPLE_MAX_CHARS]


_AI_PROMPT = (
    "下面是一段英文游戏文本。请列出其中出现的所有人名、地名、组织名与"
    "游戏专有名词（术语），每行一个，只输出名字本身，不要编号、不要解释、"
    "不要输出普通名词或句子：\n\n"
)


def _ai_candidates(
    segments: Sequence[Segment],
    ai_helper,
    source_index: Mapping[str, int],
) -> Counter:
    """信号 C：AI 识别 + 出现次数校验（防幻觉，W5c §2 信号源 C）。

    ``ai_helper`` 需提供 ``complete_text(prompt) -> str``。任何异常都静默
    吞掉——此信号是增益，不是依赖，不得成为抽取失败的原因。
    """
    if ai_helper is None:
        return Counter()
    sample = _sample_sources(segments, _AI_SAMPLE_LINES)
    if not sample:
        return Counter()
    try:
        raw = ai_helper.complete_text(_AI_PROMPT + sample)
    except Exception:  # noqa: BLE001 - 超时/无 Key/网络错都静默跳过
        return Counter()
    counts: Counter = Counter()
    for line in str(raw or "").splitlines():
        word = line.strip().lstrip("-*•\t ").strip()
        if not _is_valid_term(word):
            continue
        if word.lower() in _ENGINE_ROLES:
            continue
        # 必须在工程 source 中实际出现 >= 2 次，否则视为模型编造
        if source_index.get(word.lower(), 0) < _AI_MIN_OCCURRENCES:
            continue
        counts[word] = source_index[word.lower()]
    return counts


def _build_source_index(segments: Sequence[Segment]) -> dict[str, int]:
    """source 文本的大小写不敏感词频索引（供信号 C 校验）。"""
    index: dict[str, int] = {}
    for seg in segments:
        for tok in _WORD_RE.findall(getattr(seg, "source", "") or ""):
            index[tok.lower()] = index.get(tok.lower(), 0) + 1
    return index


# ---------------------------------------------------------------------------
# 合并与输出
# ---------------------------------------------------------------------------

def _is_valid_term(term: str) -> bool:
    """候选是否像术语：非空、非纯数字、非单字母、含英文字母、非纯虚词。"""
    t = (term or "").strip()
    if not t:
        return False
    if _is_pure_digit_or_single_letter(t):
        return False
    if not _has_english(t):
        return False
    # 单个虚词（"The" / "And"）不是术语；多词词组允许含虚词
    # （"Lord of the Rings"），因为 _is_old_string_ok 已在 B 信号里收紧。
    return t.lower() not in _STOPWORDS


def _is_pure_digit_or_single_letter(term: str) -> bool:
    """纯数字 / 单字母候选跳过。多词词组不在此列。"""
    t = (term or "").strip()
    if not t:
        return True
    if len(t) == 1 and t.isalpha():
        return True
    if t.isdigit():
        return True
    return False


def _merge(
    signal_counts: Sequence[tuple[str, Counter]],
) -> dict[str, tuple[int, str]]:
    """合并多路信号 → ``{小写键: (展示词, count, source)}``。

    - 去重键大小写不敏感，保留**首次**出现的原始大小写作展示（W5c §7.6-1）；
    - 撞名时按来源优先级取最可靠者（A > D > B > C，W5c §7.6-2），
      但 count 取各信号最大值——「出现次数」要反映真实热度，不因降级而变小。
    """
    merged: dict[str, tuple[str, int, str]] = {}
    for source, counts in signal_counts:
        for term, count in counts.items():
            key = term.lower()
            if key not in merged:
                merged[key] = (term, count, source)
                continue
            old_term, old_count, old_source = merged[key]
            new_source = (
                source if _SOURCE_RANK.get(source, 99)
                < _SOURCE_RANK.get(old_source, 99) else old_source
            )
            merged[key] = (old_term, max(old_count, count), new_source)
    return merged


# 权威信号（A/D）不占 limit；辅助信号（B/C）占 limit（W5c §7.6-3）。
_AUTHORITATIVE_SOURCES = frozenset({SOURCE_CHARACTER, SOURCE_ROLE})


def extract_candidates(
    segments: Sequence[Segment],
    profile: TermProfile | None = None,
    limit: int = 50,
    game_root: str | Path | None = None,
    ai_helper=None,
    language: str = "chinese",
) -> list[Candidate]:
    """从工程抽取候选术语（W5c §2/§3：权威定义优先 + 模型辅助）。

    四个信号源按可靠性排序：

    * **A** `define x = Character("N")` —— 扫 `game/**/*.rpy` 权威定义；
    * **D** `Segment.role` —— 解析器已拆出的说话人名，无需 `game_root`；
    * **B** `kind == "strings"` 段 —— 作者声明的 `old "X"` 短串；
    * **C** AI 识别 —— 采样对话送模型，返回词须在 source 中出现 ≥2 次。

    四路信号**全局合并去重**后再分组输出：同一个词不会因为同时被 D 与 C
    命中而出现两次，最终只保留最可靠的来源。

    ``limit`` 只约束 B/C 候选：A/D 是角色名，通常不足 30 个，**全部展示**，
    否则 50 个名额会被 B 的短串吃光、挤掉真正的人名（W5c §7.6-3）。

    排序：来源 A → D → B → C，同源按 count 降序、同次数按字典序。
    已存在于 ``profile.terms`` / ``profile.exempt_english`` 的条目被剔除
    （大小写不敏感）。
    """
    segs = list(segments or [])

    # 全局一次合并：A/D/B/C 四路信号都参与，撞名取最可靠来源。
    merged = _merge([
        (SOURCE_CHARACTER, _character_candidates(game_root, language)),
        (SOURCE_ROLE, _role_candidates(segs)),
        (SOURCE_OLD_STRING, _old_string_candidates(segs)),
        (SOURCE_AI, _ai_candidates(
            segs, ai_helper, _build_source_index(segs))),
    ])

    existing_terms: Mapping[str, str] = (
        (profile.terms or {}) if profile is not None else {}
    )
    exempt_set = {
        str(x) for x in ((profile.exempt_english or []) if profile is not None else [])
    }
    seen = {str(k).lower() for k in existing_terms}
    seen |= {x.lower() for x in exempt_set}

    authoritative: list[Candidate] = []
    auxiliary: list[Candidate] = []
    for key, (term, count, source) in merged.items():
        if key in seen:
            continue
        cand = Candidate(term, count, source)
        if source in _AUTHORITATIVE_SOURCES:
            authoritative.append(cand)
        else:
            auxiliary.append(cand)

    def _sort_key(c: Candidate):
        return (_SOURCE_RANK.get(c.source, 99), -c.count, c.term.lower())

    authoritative.sort(key=_sort_key)
    auxiliary.sort(key=_sort_key)

    if limit <= 0:
        return []
    # A/D 全量 + B/C 补足到 limit
    room = max(0, limit - len(authoritative))
    return authoritative + auxiliary[:room]



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


# --------------------------------------------------------------------------- #
# 术语表 CSV（用户易编辑格式，W5b-续）
#
# 术语表以 ``glossary.csv``（表头 原文,译名）落盘于工程目录，与 ``profile.json``
# 并存。用户或外部 AI 工具可手动编辑该 CSV；应用负责导出/导入/合并。AI 读工程
# 不在本软件内（§用户决策）。CSV 用标准库 ``csv`` 写入，自动转义
# 逗号/引号/换行，中文用 utf-8-sig 以便 Excel 打开。
# --------------------------------------------------------------------------- #

_GLOSSARY_FILENAME = "glossary.csv"
_GLOSSARY_HEADER = ("原文", "译名")


def glossary_path(work_dir: str | Path) -> Path:
    """术语表 CSV 的默认路径：``<work_dir>/glossary.csv``。"""
    return Path(work_dir) / _GLOSSARY_FILENAME


def write_glossary_csv_rows(rows: Sequence[tuple[str, str]], work_dir: str | Path) -> Path:
    """把 (原文, 译名) 行写出为 ``glossary.csv``，返回路径（导出按钮用）。

    - 按原文排序，便于人工/AI 审阅；
    - 空原文行忽略；
    - 标准库 ``csv`` 自动转义逗号/引号/换行；
    - 写 utf-8-sig（带 BOM）以兼容 Excel 打开中文。
    """
    path = glossary_path(work_dir)
    ordered = sorted(
        ((str(o), str(t)) for o, t in rows if str(o).strip()),
        key=lambda kv: kv[0],
    )
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_GLOSSARY_HEADER)
        for original, translation in ordered:
            writer.writerow([original, translation])
    return path


def write_glossary_csv(profile: TermProfile, work_dir: str | Path) -> Path:
    """由 ``TermProfile.terms`` 生成 ``glossary.csv``（导出按钮用）。"""
    return write_glossary_csv_rows(profile_to_rows(profile), work_dir)


def read_glossary_csv(work_dir: str | Path) -> list[tuple[str, str]]:
    """读取 ``glossary.csv`` → 按原文排序的 (原文, 译名) 行。

    - 文件缺失 → ``[]``（不抛错，便于首次使用）；
    - 跳过表头与空原文行；
    - 每行只取前 2 列，多列时忽略余下列。
    """
    path = glossary_path(work_dir)
    if not path.exists():
        return []
    rows: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        for i, raw in enumerate(reader):
            if i == 0 and raw and raw[:1] == list(_GLOSSARY_HEADER[:1]):
                # 表头行（首列为「原文」）跳过
                continue
            if not raw:
                continue
            original = (raw[0] or "").strip()
            if not original:
                continue
            translation = raw[1] if len(raw) > 1 else ""
            rows.append((original, translation))
    return sorted(rows, key=lambda kv: kv[0])


def merge_glossary_into_profile(
    profile: TermProfile, work_dir: str | Path
) -> TermProfile:
    """把 ``glossary.csv`` 合并进 ``profile.terms``（导入/打开位置后刷新）。

    规则：CSV 同名原文**覆盖**既有译名；CSV 新条目追加；profile 中未出现在
    CSV 的条目**保留**。返回同一 ``profile`` 实例（原地修改）。
    """
    csv_rows = read_glossary_csv(work_dir)
    if not csv_rows:
        return profile
    terms = dict(profile.terms or {})
    for original, translation in csv_rows:
        terms[original] = translation
    profile.terms = terms
    return profile
