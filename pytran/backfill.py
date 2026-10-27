"""回填与导出熔断（设计文档 §6.2、§6.3）。

本模块是**纯函数 + 数据类**，不依赖任何 UI。

* §6.2 译文来源匹配：**已退役**（DEPRECATED）——``match_translations``
  三级兜底链（uid → 特征码 → 相似度）由 ``pytran.identity.resolve_batch``
  的身份精确对位替代（DECISION(segkey-identity, 2026-10-26)，见
  docs/TASK-segkey-identity-refactor.md 与 docs/POSTMORTEM-
  misplacement-six-rounds.md）。本模块现仅保留 ``TranslationRow`` /
  ``TranslationMatch`` / ``MatchOutcome`` 数据类作兼容层、``locator_file``
  / ``normalize_feature`` 工具函数，以及 §6.3 导出熔断。
* §6.3 导出熔断：``evaluate_export_fuse`` / ``check_export_fuse`` 在导出前按文件
  统计未翻译 / 失败 / 标签校验未过 / 匹配率，任一触发即抛 ``ExportBlocked``。

DECISION(feature-file-scope, 2026-10-25)：原第 2/3 级按文件隔离的设计
（feat_path_index / feat_name_index / _same_file / _same_source 守卫）
随三级链一并删除——它们是在补「派生量当身份」的错误地基，新构造下
身份键含完整文件路径，这些洞在构造上不存在。
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from pytran.interfaces import ExportBlocked
from pytran.models import Segment, SegmentStatus
from pytran.validators import tags_ok

logger = logging.getLogger(__name__)

__all__ = [
    "TranslationRow", "TranslationMatch", "MatchOutcome",
    "normalize_feature",
    "locator_file", "match_translations", "evaluate_export_fuse",
    "check_export_fuse",
]

# 兼容别名（DEPRECATED，一个版本期）：三级兜底链及其相似度常量已随
# v3.1 segkey-identity 删除，此处仅为旧调用方 import 不炸而保留。
# 新代码禁止使用——任何相似度匹配均已被身份体系替代。
SIMILARITY_THRESHOLD = 0.92
UID_SIMILARITY_FLOOR = 0.80
UID_SIMILARITY_MIN_LEN = 12
_FULL_WIDTH_COLON = "："   # U+FF1A
_HALF_WIDTH_COLON = ":"
# §14/§6.2 归一化：'' 与 "" 都视为 "（旧数据常见引号风格混用）
_QUOTE_NORMALIZE = [("''", '"'), ('""', '"')]


def normalize_feature(feature: str) -> str:
    """§6.2 第 2 级：特征码归一化。

    去首尾空白 → ``''``/``""`` 统一为 ``"`` → 全角/半角冒号统一为半角 →
    压缩连续空白。返回用于等值比对的规范化特征码。
    """
    s = (feature or "").strip()
    for bad, good in _QUOTE_NORMALIZE:
        s = s.replace(bad, good)
    s = s.replace(_FULL_WIDTH_COLON, _HALF_WIDTH_COLON)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_LOCATOR_FILE_RE = re.compile(r"^#\s*game/(?P<path>.+?):(?P<line>\d+)\s*$")


def locator_file(locator: str) -> str:
    """从定位符 ``# game/<rel>:<line>`` 中取出文件相对路径；无法解析返回 ``""``。

    §6.2 第 2 级必须按文件隔离：``strings`` 块的 feature 是常量
    （``translate chinese2 strings:``），同一语言下**所有文件**的 strings 段
    feature 完全相同，若只按 feature 建索引就会跨文件按 FIFO 派发译文
    （2026-10-25 事故：dlc.rpy 的 43 条 strings 译文被按位置写进
    exp-1-3somes.rpy）。CSV 没有文件列，故从定位符反推。
    """
    m = _LOCATOR_FILE_RE.match((locator or "").strip())
    if not m:
        return ""
    return m.group("path").replace("\\", "/").strip()


def _file_keys(path: str) -> tuple[str, str]:
    """返回 ``(完整相对路径, 文件名)`` 两个比较键（均已归一化分隔符）。

    文件名键用于兜住历史数据里路径前缀不一致的情况（例如某次快照的
    定位符少了 ``tl/chinese2/`` 前缀，但文件名相同）。
    """
    norm = (path or "").replace("\\", "/").strip()
    return norm, norm.rsplit("/", 1)[-1] if norm else ""


def _same_file(row: "TranslationRow", seg: Segment) -> bool:
    """（已废弃，仅保留签名兼容）文件守卫随三级链删除——身份键含完整
    文件路径，构造上无跨文件命中。历史教训见 v3.1 前的本文档注释。"""
    return True


@dataclass
class TranslationRow:
    """一条外来译文（迁移导入 / CSV 手改），待匹配回工程 Segment。"""

    uid: Optional[int]
    feature: str
    locator: str
    source: str
    target: str


@dataclass
class TranslationMatch:
    """``match_translations`` 的命中结果。"""

    row: "TranslationRow"
    segment: Segment
    method: str  # "uid" | "feature" | "similarity"


@dataclass
class MatchOutcome:
    """``match_translations`` 的完整结果。"""

    matches: list[TranslationMatch]
    unmatched: list[TranslationRow]


def match_translations(
    segments: Iterable[Segment],
    rows: Iterable[TranslationRow],
    *,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    uid_similarity_floor: float = UID_SIMILARITY_FLOOR,
    on_warning: Optional[Callable[[str, TranslationRow, Segment], None]] = None,
    use_uid_matching: bool = True,
) -> MatchOutcome:
    """**已退役**（DEPRECATED，任务书 v3.1 §2.3）——薄兼容包装，转调
    ``pytran.identity.resolve_batch``。

    DECISION(segkey-identity, 2026-10-26)：六轮复盘证明三级兜底链
    （uid → 特征码 → 相似度）本身就是错位形态：每个"高级别"的正确性
    都依赖未验证的外部前提，每层兜底都是错位工厂（§3.2）。身份对位
    统一收编到 ``identity.resolve_batch``：按 (file, source) 精确解析，
    查不到即 unmatched，**不存在猜测**。

    本包装保留一个版本期供旧调用方过渡；``similarity_threshold`` /
    ``uid_similarity_floor`` / ``on_warning`` / ``use_uid_matching`` 参数
    被忽略（相似度匹配已整体移除）。下个版本删除本函数。
    """
    from pytran.identity import resolve_batch

    logger.warning(
        "match_translations 已退役（DEPRECATED），请改用 pytran.identity.resolve_batch"
    )
    outcome = resolve_batch(segments, rows)
    if outcome.unmatched and on_warning is not None:
        for row in outcome.unmatched:
            on_warning("identity_unmatched", row, None)  # type: ignore[arg-type]
    return MatchOutcome(
        matches=[
            # 老契约需要 method 字段；身份对位无"级别"概念，统一 "identity"
            TranslationMatch(row=m.row, segment=m.segment, method="identity")
            for m in outcome.matches
        ],
        unmatched=list(outcome.unmatched),
    )


def evaluate_export_fuse(
    project,
    *,
    match_rate: Optional[float] = None,
) -> dict[str, list[str]]:
    """§6.3 导出熔断评估：返回阻塞明细 ``dict[str, list[str]]``，空字典表示可导出。

    触发条件（任一即阻止）：
    * ``untranslated``：存在无译文的行（target 为空）；
    * ``failed``：存在 ``status=failed`` 行；
    * ``tag_check``：存在 target 非空但标签/插值/转义计数与 source 不一致的行；
    * ``match_rate``：迁移导入场景下匹配率 < 0.95（由调用方传入）。

    DECISION(segkey-identity, v3.1 §2.5)：内容级不变量
    ``seg.source == row.source`` 在 ``identity.resolve_batch`` 的**构造点**
    强制断言（IdentityViolation），不在写回时补查——错位若可能发生，
    在匹配发生的那一刻就已爆炸。本函数维持按段自检（标签/插值/转义）。
    """
    details: dict[str, list[str]] = {}
    untranslated: list[str] = []
    failed: list[str] = []
    tag_fail: list[str] = []

    for seg in project.segments:
        if seg.status == SegmentStatus.SKIPPED:
            continue
        has_target = bool(seg.target and seg.target.strip())
        if not has_target:
            untranslated.append(f"uid={seg.uid}")
        if seg.status == SegmentStatus.FAILED:
            failed.append(f"uid={seg.uid}")
        if has_target and not tags_ok(seg.source, seg.target):
            tag_fail.append(f"uid={seg.uid}")

    if untranslated:
        details["untranslated"] = untranslated
    if failed:
        details["failed"] = failed
    if tag_fail:
        details["tag_check"] = tag_fail
    if match_rate is not None and match_rate < 0.95:
        details["match_rate"] = [f"匹配率={match_rate:.4f} < 0.95"]
    return details


def check_export_fuse(project, *, match_rate: Optional[float] = None) -> None:
    """§6.3 导出熔断：存在阻止项时抛 ``ExportBlocked(明细)``。"""
    details = evaluate_export_fuse(project, match_rate=match_rate)
    if details:
        raise ExportBlocked("导出熔断：存在阻止项（§6.3）", details)
