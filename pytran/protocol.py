"""请求构建 + 响应三层解析 + §5.4 申报护栏（设计文档 §4.1、§4.2、§5.4、ADR-0002）。

与上下游的边界：
- 上游（引擎 §4.3）给「一批编号行」，本模块只负责把它变成 messages、再把模型回文
  变成 `dict[uid, str|KeepDeclared]`；
- 编号缺失/多余等 V1 判定在此完成（这里才有"批"的概念），
  行内文本质量判定交给 `validators.Validator`；
- 本模块不发起网络请求（那是 providers.py），因此可在无网络下完整测试。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from pytran.config import Config, TermProfile
from pytran.interfaces import KeepDeclared, ProtocolError
from pytran.validators import check_ids

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SYSTEM_PROMPT", "KEEP_DECLARE_HINT", "MAX_CONTEXT_LINES",
    "ParseResult", "build_system_prompt", "build_user_prompt", "build_messages",
    "build_request_params", "normalize_keep_flag", "parse_response",
    "MAX_CONTEXT_LINES", "KEEP_DECLARE_RATE_MIN_ROWS",
]

# §4.1 系统提示词（设计文档原文五条规则；OpenAI 标准 messages 结构）
DEFAULT_SYSTEM_PROMPT = (
    "你是专业的游戏本地化译者。用户会给你若干行带编号的 Ren'Py 脚本文本，格式为「编号|文本」。\n"
    "请严格按以下规则输出：\n"
    "1. 只输出 JSON 行，每行一个：{\"i\": <编号>, \"t\": \"<译文>\"}；不要输出任何解释、空行或代码块。\n"
    "2. 编号必须与输入完全一致，不得增删、不得改序，不得翻译编号或把它写进译文。\n"
    "3. 完整翻译每一行，不要省略、不要合并、不要拆分；保留全部 {w} 等待标签、"
    "[player_name] 插值与 \\n 转义，数量与原文一致。\n"
    "4. 译文使用中文标点；禁止输出英文双引号（需要时用「」或『』）。\n"
    "5. 术语与人名按给定的术语表译；术语表未覆盖且原文即为人名/专名的，保留原文。"
)

# §5.4 申报提示：只在重试轮的小请求注入（首轮保持最简）
KEEP_DECLARE_HINT = (
    "若某行确实无需翻译——人名/拟声词/原文即为有意保留的英文——"
    "请输出 {\"i\":n,\"t\":\"<原文>\",\"k\":1,\"r\":\"一句话原因\"}"
)

MAX_CONTEXT_LINES = 3

# §5.4 护栏 4 的最小样本量：低于此行数的批次不启用申报率闸。
# DECISION(§5.4 护栏4)：申报只在**重试轮的小请求**（几行）注入，而 15% 阈值是按
# 正常批（数十行）标定的——3 行里申报 1 行即 33%，会 100% 触发降级，令申报机制
# 完全失效。故设行数下限：小样本交给护栏 1/2（逐条人工裁定）把关，
# 大样本才启用比例闸。这是把「比例闸」放回它真正能工作的口径上。
KEEP_DECLARE_RATE_MIN_ROWS = 8

# §4.2 第二层：JSON 行级正则（容忍非法转义，如 \q）
_JSON_LINE_RE = re.compile(r'\{\s*"i"\s*:\s*(\d+)\s*,\s*"t"\s*:\s*"((?:[^"\\]|\\.)*)"')
# §4.2 第三层：退化成「编号|文本」
_NUMBERED_RE = re.compile(r"^(\d+)\|(.*)$")
_FENCE_RE = re.compile(r"^```[A-Za-z0-9_+\-]*$")

_TRUE_VARIANTS = {"1", "true", "yes", "y", "keep", "on", "t"}
_FALSE_VARIANTS = {"0", "false", "no", "n", "off", "f", ""}


@dataclass
class ParseResult:
    """§4.2 解析结果 + §5.4 申报处置记录。"""

    results: dict[int, str | KeepDeclared]
    missing: list[int] = field(default_factory=list)   # 请求了但没返回（缺行）
    extra: list[int] = field(default_factory=list)     # 未请求的编号（丢弃）
    duplicates: list[int] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)  # 三层都救不回来的原始行
    declared: list[int] = field(default_factory=list)  # §5.4 被接受的申报
    rejected: dict[int, str] = field(default_factory=dict)  # 申报但文本被改 → 原因码
    downgraded: list[int] = field(default_factory=list)     # 护栏 4 批量降级
    alert: str | None = None


def build_system_prompt(profile: TermProfile | None = None) -> str:
    """§4.1：默认提示词 + profile 追加/覆盖段（`prompt_overrides`）。"""
    if profile is None:
        return DEFAULT_SYSTEM_PROMPT
    overrides: Mapping[str, str] = getattr(profile, "prompt_overrides", None) or {}
    append = str(overrides.get("system_append", "")).strip()
    return f"{DEFAULT_SYSTEM_PROMPT}\n{append}" if append else DEFAULT_SYSTEM_PROMPT


def build_user_prompt(
    numbered_lines: Sequence[tuple[int, str]],
    profile: TermProfile | None = None,
    context: Sequence[tuple[int, str, str]] = (),
    allow_keep_declare: bool = False,
) -> str:
    """§4.1 用户提示词：术语表 → 前文参考（2–3 条）→ 编号行。"""
    blocks: list[str] = []

    terms: Mapping[str, str] = getattr(profile, "terms", None) or {}
    if terms:
        lines = [f"{k}={v}" for k, v in terms.items()]
        blocks.append("术语表：\n" + "\n".join(lines))

    if context:
        # §4.1「前文参考 2–3 条」。DECISION(§4.1)：超出上限时保留**最新的** N 条——
        # 与待翻译行距离越近的上下文对语气/称谓一致性越有价值，旧句子只会挤占 token。
        recent = list(context)[-MAX_CONTEXT_LINES:]
        lines = [f"{uid}|{src}\n  → {dst}" for uid, src, dst in recent]
        blocks.append("前文参考：\n" + "\n".join(lines))

    body = "\n".join(f"{uid}|{text}" for uid, text in numbered_lines)
    blocks.append(f"待翻译（{len(numbered_lines)} 行）：\n{body}")

    if allow_keep_declare:
        blocks.append(KEEP_DECLARE_HINT)

    return "\n\n".join(blocks)


def build_messages(
    numbered_lines: Sequence[tuple[int, str]],
    profile: TermProfile | None = None,
    context: Sequence[tuple[int, str, str]] = (),
    allow_keep_declare: bool = False,
) -> list[dict[str, str]]:
    """OpenAI 标准 `messages`（只有 system/user 两角色，§4.1）。"""
    return [
        {"role": "system", "content": build_system_prompt(profile)},
        {
            "role": "user",
            "content": build_user_prompt(numbered_lines, profile, context, allow_keep_declare),
        },
    ]


def build_request_params(
    config: Config,
    messages: Sequence[Mapping[str, str]],
    use_fallback_model: bool = False,
) -> dict:
    """§4.1 请求参数：只用 OpenAI 标准字段（`temperature=0.2`/`top_p=0.9`/`timeout=120`）。

    注意：`api_key`/`base_url` 属于客户端构造参数，不进请求体（§12 密钥不落盘/不回显）。
    """
    model = config.fallback_model if use_fallback_model and config.fallback_model else config.model
    params: dict = {
        "model": model,
        "messages": [dict(m) for m in messages],
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
        "timeout": float(config.timeout),
    }
    return params


def normalize_keep_flag(value: object) -> bool | None:
    """§5.4 护栏 3：`k` 字段归一化，`1/true/"keep"/yes` → True；未知变体 → None。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
            # 0 → False 走下面字符串分支也无妨，这里直接判定
        if value == 0:
            return False
        return None
    text = str(value).strip().lower()
    if text in _TRUE_VARIANTS:
        return True
    if text in _FALSE_VARIANTS:
        return False
    return None


def _strip_fence_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or _FENCE_RE.match(stripped):
            continue
        # 行内围栏（```json ... ``` 写在一行）
        m = re.match(r"^```[A-Za-z0-9_+\-]*\s*(.*?)\s*```$", stripped)
        if m:
            stripped = m.group(1)
        lines.append(stripped)
    return lines


def _decode_escapes(text: str) -> str:
    """把 `\\n`/`\\"`/`\\\\` 还原为真实字符；非法转义（\\q）原样保留（§4.2 第二层）。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
        if nxt in mapping:
            out.append(mapping[nxt])
            i += 2
        elif nxt == "u" and i + 5 < len(text) + 1:
            try:
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
            out.append(ch)
            i += 1
        else:
            out.append(ch + nxt)  # 非法转义原样保留，不丢字符
            i += 2
    return "".join(out)


def _parse_one_line(line: str) -> tuple[int, str, object] | None:
    """三层解析单行，返回 `(uid, text, raw_k)`；三层都失败返回 None。"""
    # a) json.loads（最严格，结构完整时优先）
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict) and "i" in obj and "t" in obj:
        try:
            return int(obj["i"]), _to_text(obj["t"]), obj.get("k", False)
        except (TypeError, ValueError):
            return None

    # b) 行内 JSON 正则（容忍非法转义/多余空白）
    m = _JSON_LINE_RE.search(line)
    if m:
        k_val: object = False
        km = re.search(r'"k"\s*:\s*("?[^",}]+"?)', line[m.end():])
        if km:
            raw_k = km.group(1).strip().strip('"')
            k_val = raw_k if raw_k else 1
        return int(m.group(1)), _decode_escapes(m.group(2)), k_val

    # c) 退化成「编号|文本」
    m = _NUMBERED_RE.match(line)
    if m:
        return int(m.group(1)), m.group(2).strip(), False

    return None


def _to_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def parse_response(
    raw: str,
    requested_ids: Sequence[int],
    allow_keep_declare: bool = False,
    sources: Mapping[int, str] | None = None,
    keep_declare_rate_limit: float = 0.15,
) -> ParseResult:
    """§4.2 三层解析 + §5.4 四道护栏。

    参数：
    - `requested_ids`：本批请求的编号（V1 判定基准，多余编号一律丢弃）；
    - `allow_keep_declare`：是否注入了申报选项（§5.4 注入时机——只有重试轮才为 True）；
    - `sources`：`uid → 原文`，护栏 2 校验 `t==source` 必需；
    - `keep_declare_rate_limit`：护栏 4 的批次申报率阈值（默认 0.15）。

    DECISION(§4.3)：三层**全部**失败（连一行都救不回来）才抛 `ProtocolError`；
    只要解析出行就不抛，未解析出的行记入 `unparsed` 且对应编号进 `missing`，
    由引擎按 §5.3「逐行错误原因」定点重发——这比整批失败更省 token，
    也避免模型一次啰嗦就丢掉整批已正确翻译的内容。
    """
    requested = list(requested_ids)
    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolError("响应为空：无法解析任何编号行")

    parsed: list[tuple[int, str, object]] = []
    unparsed: list[str] = []
    for line in _strip_fence_lines(raw):
        item = _parse_one_line(line)
        if item is None:
            unparsed.append(line)
        else:
            parsed.append(item)

    if not parsed:
        raise ProtocolError(f"响应中没有任何可解析的编号行（原始响应前 200 字符：{raw[:200]!r}）")

    # V1：丢弃未请求的编号；重复编号保留首次（§5 表 V1「多余丢弃」）
    wanted = set(requested)
    results: dict[int, str | KeepDeclared] = {}
    duplicates: list[int] = []
    returned: list[int] = []
    pending_keep: dict[int, tuple[str, str]] = {}  # uid → (text, reason)
    for uid, text, raw_k in parsed:
        if uid in returned:
            if uid not in duplicates:
                duplicates.append(uid)
            continue
        returned.append(uid)
        if uid not in wanted:
            continue
        flag = normalize_keep_flag(raw_k)
        if allow_keep_declare and flag is True:
            pending_keep[uid] = (text, "")
        else:
            # 未申报 / 未注入申报选项 / 未知变体（护栏 3）→ 一律按普通文本
            results[uid] = text
    if pending_keep:
        reasons = _extract_reasons(raw)
        pending_keep = {uid: (text, reasons.get(uid, "")) for uid, (text, _) in pending_keep.items()}

    # §5.4 护栏 4：批次申报率超阈值 → 全部申报降级为普通文本并告警。
    # DECISION(§5.4 护栏4)：分母取**本批实际返回行数**——护栏测的是"模型对已产出
    # 内容的偷懒比例"，返回行数即它声称完成的工作量；用请求行数做分母时，模型
    # 少返回（缺行）反而会稀释申报率，等于奖励少答。缺行由 V1 单独处理，不重复惩罚。
    downgraded: list[int] = []
    alert: str | None = None
    kept_rows = [uid for uid in returned if uid in wanted]
    if pending_keep and kept_rows and len(kept_rows) >= KEEP_DECLARE_RATE_MIN_ROWS:
        rate = len(pending_keep) / len(kept_rows)
        if rate > keep_declare_rate_limit:
            downgraded = sorted(pending_keep)
            for uid, (text, _reason) in pending_keep.items():
                results[uid] = text
            alert = (
                f"AI 申报率 {rate:.0%}（{len(pending_keep)}/{len(kept_rows)}）"
                f"超过阈值 {keep_declare_rate_limit:.0%}：本批申报全部降级为普通译文并重译"
            )
            logger.warning("§5.4 护栏4 触发：%s", alert)
            pending_keep = {}

    # §5.4 护栏 1/2/3：逐条处置申报
    declared: list[int] = []
    rejected: dict[int, str] = {}
    for uid, (text, reason) in sorted(pending_keep.items()):
        source = (sources or {}).get(uid)
        # 护栏 2：t 必须等于原文（或空），否则申报不成立 → 按 UNTRANSLATED 重译
        if source is not None and text.strip() not in (source.strip(), ""):
            results[uid] = text
            rejected[uid] = "declared_but_changed"
            logger.info("§5.4 护栏2：uid=%s 申报但文本被改动，按未翻译处理", uid)
            continue
        if source is None:
            # 无原文可比对 → 不得放行（护栏 2 的保守解释）
            results[uid] = text
            rejected[uid] = "declared_without_source"
            continue
        # 护栏 1：合法申报 → KeepDeclared 哨兵进人工队列，**绝不自动 PASS**
        results[uid] = KeepDeclared(reason or "AI 申报无需翻译")
        declared.append(uid)

    missing, extra, dup = check_ids(requested, returned)
    if dup:
        duplicates = sorted(set(duplicates) | set(dup))
    return ParseResult(
        results=results,
        missing=missing,
        extra=extra,
        duplicates=duplicates,
        unparsed=unparsed,
        declared=declared,
        rejected=rejected,
        downgraded=downgraded,
        alert=alert,
    )


def _extract_reasons(raw: str) -> dict[int, str]:
    """从原始响应里抓 `"r"` 字段（只需 uid→原因，容忍非法转义）。"""
    reasons: dict[int, str] = {}
    for line in _strip_fence_lines(raw):
        m = _JSON_LINE_RE.search(line)
        if not m:
            continue
        uid = int(m.group(1))
        r = re.search(r'"r"\s*:\s*"((?:[^"\\]|\\.)*)"', line)
        if r:
            reasons[uid] = _decode_escapes(r.group(1))
    return reasons
