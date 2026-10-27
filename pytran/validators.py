"""V1–V6 校验、自动修复与漏翻检测器（设计文档 §5、§5.1–§5.4、ADR-0001）。

职责边界：
- 只做「单行译文」的词法/结构判定与自动修复，**不认识**段、文件、项目；
- 检测器是**纯函数式**的：不读任何全局配置，豁免依据全部来自参数 `profile`
  （任务 W2 特别约束），因此同一实例可在 §4.3 的并发线程中安全复用；
- 编号层面的 V1 由 `protocol.parse_response()` 负责（那里才有"批"的概念），
  本模块只提供 `check_ids()` 供其调用。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from pytran.config import TermProfile
from pytran.interfaces import Detection

__all__ = [
    "TAG_RE", "VAR_RE", "ESC_RE", "WORD_RE",
    "Whitelist", "CheckResult",
    "strip_fences", "repair_quotes", "tags_ok", "tag_messages",
    "compute_residual", "is_interjection_like", "check_ids",
    "Validator",
]

# --- §5.2 标签/插值/转义 ------------------------------------------------------
TAG_RE = re.compile(r"\{[^}]*\}")        # {w} {b} {color=#fff} {/b} {w=0.5}
VAR_RE = re.compile(r"\[[^\]]*\]")       # [player_name] [config.name!t]
# Ren'Py 变量/插值标签「可译」修饰符 `!t`：只用于方括号写法（如 [e_name!t]），
# 仅声明该标签内容可被翻译，不改变标签身份——[e_name] 与 [e_name!t] 是同一标签。
# 花括号文本标签 {w} 等没有 `!t` 这种写法，故只对方括号做归一化。
_T_MOD_RE = re.compile(r"!t(?=\])")
ESC_RE = re.compile(r"\\.")              # \n \" \\

# BUGFIX(quote-esc-equivalence, 2026-10-26)：引号类转义（\"）在英→中翻译中
# 合法地变为中文引号『』「」《》等——按字面计数严格相等会把**正确的翻译**
# 误报为 tag_check（用户实测 10 条误报中 6 条属此类）。转义分两级：
# * 结构性转义（_STRUCT_ESC）：\\n \\\\ 等，影响 Ren'Py 文本结构，必须计数相等；
# * 引号转义（\\"）：译文侧用中文引号替代即视为等价——原文每出现一个 \\"，
#   译文只需存在任意中文引号字符即抵消；反向（原文无、译文有）同样放行，
#   因为人工增注中文引号不改变文本结构。
_QUOTE_ESC = '\\"'
_STRUCT_ESC_RE = re.compile(r"\\(?:[^\\\"]|\\\\)")   # 除 \" 外的转义（含 \\ 与 \n）
_CJK_QUOTE_CHARS = "「」『』《》〈〉【】（）‘’“”＇"

# --- §5.1/§5.3 英文词判定 ----------------------------------------------------
_LETTERS = "A-Za-z\u00c0-\u024f\u0400-\u04ff"
WORD_RE = re.compile(f"[{_LETTERS}][{_LETTERS}'\\-]*")
# 纯标点/数字/符号行：target == source 时视为"无实词"（§5.3 PASS 条件）
_PUNCT_RE = re.compile(r"^[\s\W_0-9]+$", re.UNICODE)
_TOKEN_RE = re.compile(f"^([{_LETTERS}]+)([\\W_0-9]*)$", re.UNICODE)
_REPEAT_RE = re.compile(r"([A-Za-z])\1{1,}")
# 标识符/常量/单字母键位形态：§5.1「原文中同样整词出现」豁免只对这类生效（见 compute_residual）
_IDENT_RE = re.compile(r"^(?:[A-Za-z]|[A-Z][A-Z0-9_]{2,}|[A-Za-z0-9]*(?:_|\.)[A-Za-z0-9_]+)$")


@dataclass(frozen=True)
class Whitelist:
    """§5.1 放行白名单（纯数据；由 profile 构造，不读全局配置）。

    优先级：terms > exempt > 原文同词（标识符形态） > ≤3 字母缩写（开关）。
    """

    terms: tuple[str, ...] = ()
    exempt: tuple[str, ...] = ()
    allow_short_abbrev: bool = False

    @classmethod
    def from_profile(cls, profile: TermProfile | None) -> "Whitelist":
        """由 profile 构造白名单（`profile=None` 等价于空表）。"""
        terms: Iterable[str] = getattr(profile, "terms", None) or {}
        exempt = getattr(profile, "exempt_english", None) or []
        overrides = getattr(profile, "prompt_overrides", None) or {}
        return cls(
            terms=tuple(sorted((str(k) for k in getattr(terms, "keys", terms)()), key=len, reverse=True))
            if hasattr(terms, "keys") else tuple(sorted((str(k) for k in terms), key=len, reverse=True)),
            exempt=tuple(sorted((str(x) for x in exempt), key=len, reverse=True)),
            allow_short_abbrev=str(overrides.get("allow_short_abbrev", "")).lower() == "true",
        )

    def allows(self, token: str) -> bool:
        """术语表/豁免列表/≤3 字母缩写开关（"原文同词"需要上下文，见 compute_residual）。"""
        low = token.lower()
        if any(t.lower() == low for t in self.terms):
            return True
        if any(e.lower() == low for e in self.exempt):
            return True
        if self.allow_short_abbrev and len(token) <= 3:
            return True
        return False


@dataclass
class CheckResult:
    """`check_detailed()` 的结构化结果（引擎据此生成逐行错误说明，§5.3 第 1 次定点升级）。"""

    detection: str
    repaired: str
    errors: list[str] = field(default_factory=list)      # 失败原因码（§5 表）
    residual: list[str] = field(default_factory=list)    # 剔除放行项后残留的英文
    preserved: list[str] = field(default_factory=list)   # 命中豁免的保留项（重发时注明）


def strip_fences(text: str) -> str:
    """V3：剥离 ``` 围栏与首尾空白（**绝不动正文内部空白**，§5.2）。"""
    out = (text or "").strip()
    m = re.match(r"^```[^\n]*\n(?P<body>.*?)\n?```$", out, re.DOTALL)
    if m:
        return m.group("body").strip()
    m = re.match(r"^```(?P<body>.*)```$", out, re.DOTALL)
    if m:
        return m.group("body").strip()
    # 单侧围栏（模型只写了开头或结尾）
    out = re.sub(r"^```[A-Za-z0-9_+\-]*\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    return out.strip()


def repair_quotes(text: str) -> str:
    """V4：英文双引号成对替换为「」/『』（沿用 2.0 check-UI 的交替算法，嵌套层用『』）。"""
    if '"' not in text:
        return text
    # DECISION(§5 V4)：沿 2.0「交替替换」思路，但用「前一个字符是否为词内字符」区分
    # 开/闭（2.0 只看奇偶，`他说"你好"` 与 `"外层 "内层" 继续"` 会错位）：
    #   开引号 ← 行首/空白/已开的「『 之后；闭引号 ← 词内字符之后。
    pairs = (("「", "」"), ("『", "』"))
    openers = "「『（([{"
    out: list[str] = []
    stack: list[str] = []
    for idx, ch in enumerate(text):
        if ch != '"':
            out.append(ch)
            continue
        prev = text[idx - 1] if idx else ""
        is_open = (not stack) or (not prev) or prev.isspace() or prev in openers
        if is_open:
            opener, closer = pairs[min(len(stack), len(pairs) - 1)]
            out.append(opener)
            stack.append(closer)
        else:
            out.append(stack.pop() if stack else "」")
    return "".join(out)


def _norm_tags(text: str) -> dict[re.Pattern, Counter]:
    """按 TAG/VAR/ESC 三类分别计数；方括号变量标签比较前剥离 Ren'Py 的
    `!t` 修饰符（[e_name!t] → [e_name]）。花括号标签 {w} 无 `!t` 写法，不归一化。

    BUGFIX(quote-esc-equivalence, 2026-10-26)：计数时把 `\\"`（引号转义）
    从转义桶中拆出——它由 :func:`tags_ok` 按引号等价规则单独比较，
    不参与字面计数（否则正确的引号风格转换被误报）。
    """
    return {
        TAG_RE: Counter(TAG_RE.findall(text or "")),
        VAR_RE: Counter(_T_MOD_RE.sub("", t) for t in VAR_RE.findall(text or "")),
        ESC_RE: Counter(
            t for t in ESC_RE.findall(text or "") if t != _QUOTE_ESC
        ),
    }


def _quote_esc_balance(src: str, dst: str) -> bool:
    """引号转义等价判定（最终版，BUGFIX quote-esc-direction 2026-10-26）。

    引号（无论 \\" 转义还是中文引号字符）**不改变 Ren'Py 文本结构**——
    写回时按字面还原，引擎按文本内容渲染。因此：
    * 数量不等 → 放行（译文补 \\" 强调、或改写引号风格，均为合法修辞；
      用户实测 10 条误报全部属于此类）；
    * 唯一保留的检查：不因引号改写而丢失**其它**结构性转义——那已由
      _norm_tags 的结构桶（过滤掉 \\" 后）覆盖，这里恒通过。

    即：引号类转义完全退出计数校验。函数保留是为了让 tag_messages 的
    提示与防线结构稳定，未来若需恢复更严口径只改此一处。
    """
    return True


def tags_ok(src: str, dst: str) -> bool:
    """V6：标签/插值/转义计数相等（允许换位置，§5.2）。

    Ren'Py 的 `!t` 翻译修饰符（如 [e_name] 与 [e_name!t]）视为同一标签，
    比较前归一化掉。

    BUGFIX(quote-esc-equivalence, 2026-10-26)：引号转义 \\" 不参与字面
    计数，改由引号等价规则（:func:`_quote_esc_balance`）判定——英→中
    翻译把 \\" 改写为『』「」等中文引号是正确行为，不得拦。
    """
    s, d = _norm_tags(src), _norm_tags(dst)
    if not all(s[rx] == d[rx] for rx in (TAG_RE, VAR_RE, ESC_RE)):
        return False
    return _quote_esc_balance(src, dst)


def tag_messages(src: str, dst: str) -> list[str]:
    """V6 失败原因明细（§5 表：`tags: {w} 原文2处译文1处`）。"""
    msgs: list[str] = []
    s, d = _norm_tags(src), _norm_tags(dst)
    for rx in (TAG_RE, VAR_RE, ESC_RE):
        for token in sorted(set(s[rx]) | set(d[rx])):
            if s[rx].get(token, 0) != d[rx].get(token, 0):
                msgs.append(f"tags: {token} 原文{s[rx].get(token, 0)}处译文{d[rx].get(token, 0)}处")
    if not _quote_esc_balance(src, dst):
        msgs.append(
            f"tags: {_QUOTE_ESC} 引号转义与中文引号不平衡"
            f"（原文 {src.count(_QUOTE_ESC)} 处 + 中文引号，"
            f"译文 {dst.count(_QUOTE_ESC)} 处 + 中文引号）")
    return msgs


def is_interjection_like(source: str) -> bool:
    """§5.3 边界情形：原文是单个感叹/拟声 token（`Hahhh...`、`Ugh!`）。"""
    text = (source or "").strip()
    if not text or re.search(r"\s", text):
        return False
    m = _TOKEN_RE.match(text)
    if not m:
        return False
    word, tail = m.group(1), m.group(2)
    if len(word) > 12:
        return False
    return bool(_REPEAT_RE.search(word)) or len(tail) >= 2 or (tail and tail[0] in "!?")


# --- V7：已删除（用户要求 2026-10-24） --------------------------------------
# 原「编号错配」启发式检测（译文重复 / 相邻长度倒挂 / 尖叫行交叉 + 单行
# 长度兜底）全部移除。理由：三类指纹都只是**推断**，既有漏报也会误报——
# 用户明确要求「能够 100% 判断错配的方法」，而批次内错配在信息上不可能
# 被验证。改由「全面审校」面板的「AI 校对」按钮做全文逐行对照复核
# （见 pytran/review.py），结论并入同一张表、共用「重译」。


def compute_residual(
    source: str, target: str, whitelist: Whitelist
) -> tuple[list[str], list[str]]:
    """§5.3 固定顺序：先剥保留结构 → 词级白名单匹配 → 残余。

    返回 `(residual, preserved)`：
    - residual：剔除放行项后仍残留的英文词（决定行级结论）；
    - preserved：命中豁免的保留项（重发请求中注明，避免模型把保留项当待翻内容）。

    DECISION(§5.1)：「该英文片段同样出现在原文中且为整词」这条豁免，设计给的例子
    （`[var]` 内部、`{w=0.5}` 参数）本就被 V6 剥离；若对普通英文词也生效，则
    "Hello there." 原样返回会被整行豁免，与 §5.3 的 UNTRANSLATED 判定冲突。
    故本条只对**标识符/常量/单字母键位**形态生效（`E`、`HP_BAR`、`config.name`）。
    """
    stripped = ESC_RE.sub(" ", VAR_RE.sub(" ", TAG_RE.sub(" ", target or "")))
    source_words = {w.lower() for w in WORD_RE.findall(source or "")}
    residual: list[str] = []
    preserved: list[str] = []
    seen: set[str] = set()
    for token in WORD_RE.findall(stripped):
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        if whitelist.allows(token):
            preserved.append(token)
        elif _IDENT_RE.match(token) and key in source_words:
            preserved.append(token)
        else:
            residual.append(token)
    return residual, preserved


def check_ids(
    requested: Sequence[int], returned: Sequence[int]
) -> tuple[list[int], list[int], list[int]]:
    """V1：编号缺失/多余/重复；多余丢弃（§5 表 V1）。返回 `(missing, extra, duplicates)`。"""
    seen: set[int] = set()
    duplicates: list[int] = []
    for uid in returned:
        if uid in seen and uid not in duplicates:
            duplicates.append(uid)
        seen.add(uid)
    wanted = set(requested)
    missing = [uid for uid in requested if uid not in seen]
    extra = [uid for uid in dict.fromkeys(returned) if uid not in wanted]
    return missing, extra, duplicates


class Validator:
    """V1–V6 校验 + 自动修复 + 四态漏翻检测（§5、ADR-0001）。无内部状态，可并发复用。"""

    def __init__(self, allow_short_abbrev: bool = False) -> None:
        # DECISION(§5.1)：≤3 字母缩写豁免是配置开关。TermProfile 已冻结（T0），
        # 故支持两处开启：实例构造参数，或 profile.prompt_overrides["allow_short_abbrev"]。
        self.allow_short_abbrev = allow_short_abbrev

    # -- 契约方法（interfaces.Validator） -------------------------------------
    def check(
        self,
        source: str,
        target: str,
        profile: TermProfile,
        allow_keep_declare: bool = False,
    ) -> tuple[str, str | None]:
        """返回 `(detection, repaired_target)`。纯函数：不读全局配置。"""
        result = self.check_detailed(source, target, profile, allow_keep_declare)
        return result.detection, result.repaired

    # -- 结构化入口 -----------------------------------------------------------
    def check_detailed(
        self,
        source: str,
        target: str,
        profile: TermProfile,
        allow_keep_declare: bool = False,
    ) -> CheckResult:
        """完整判定，供引擎生成"逐行错误原因"重发请求（§5.3 第 1 次定点升级）。"""
        wl = Whitelist.from_profile(profile)
        if self.allow_short_abbrev and not wl.allow_short_abbrev:
            wl = Whitelist(wl.terms, wl.exempt, True)

        # V3（围栏/首尾空白）→ V4（英文双引号）：先自动修，修不了才算失败
        repaired = repair_quotes(strip_fences(target or ""))

        # V2：空译文
        if not repaired.strip():
            return CheckResult(Detection.EMPTY.value, repaired, ["empty"])

        errors: list[str] = []
        # V6：标签/插值/转义计数
        struct_msgs = tag_messages(source or "", repaired)
        errors.extend(struct_msgs)

        # V5/§5.3：词级白名单 → 残余 → 行级结论（ADR-0001 固定顺序）
        residual, preserved = compute_residual(source or "", repaired, wl)
        if residual:
            errors.append("english")

        same = repaired.strip() == (source or "").strip()
        if residual:
            # DECISION(§5.3)：target==source 且剔除豁免后仍有实词 → UNTRANSLATED，否则 RESIDUAL
            detection = Detection.RESIDUAL.value
            if same and not self._is_pass_source(source or "", wl, profile):
                detection = Detection.UNTRANSLATED.value
            elif same and self._is_pass_source(source or "", wl, profile):
                detection = Detection.PASS.value
        else:
            detection = Detection.PASS.value

        # DECISION(§5/§5.3)：四态面向「漏翻」，V6 结构错误没有对应态；Detection 已冻结，
        # 故把「结构错误且残余为空」提升为 RESIDUAL（译文不可用 → 入重译队列），
        # 明细由 errors 携带。契约变更建议见交付说明。
        if detection == Detection.PASS.value and struct_msgs:
            detection = Detection.RESIDUAL.value

        return CheckResult(detection, repaired, errors, residual, preserved)

    # -- 内部 -----------------------------------------------------------------
    def _is_pass_source(self, source: str, wl: Whitelist, profile: TermProfile | None) -> bool:
        """§5.3 PASS 条件：target==source 但剔除豁免后无实词（纯标点/符号/拟声占位）。"""
        if _PUNCT_RE.match(source):
            return True
        if is_interjection_like(source) and self._interjection_mode(profile) == "pass":
            # DECISION(§5.3)：拟声/感叹行的 profile 级开关。TermProfile 已冻结，
            # 借既有的 prompt_overrides 通道传递（"interjection": "pass"）。
            return True
        src_residual, _ = compute_residual(source, source, wl)
        return not src_residual

    @staticmethod
    def _interjection_mode(profile: TermProfile | None) -> str:
        overrides = getattr(profile, "prompt_overrides", None) or {}
        return str(overrides.get("interjection", "queue")).lower()
