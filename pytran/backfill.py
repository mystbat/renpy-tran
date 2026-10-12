"""回填与导出熔断（设计文档 §6.2、§6.3，W3 交付物之一）。

本模块是**纯函数 + 数据类**，不依赖任何 UI，便于单测，也是 W5 的
``import_csv`` / 迁移导入回填译文的底层支撑：

* §6.2 译文来源兜底链：``match_translations`` 对每条外来译文（迁移导入 / 用户手改
  CSV）按 ``uid 精确 → 特征码归一化 → 相似度≥0.92 且定位符相同`` 三级兜底匹配到
  当前工程的 Segment；三级都失败则判为未匹配。
* §6.3 导出熔断：``evaluate_export_fuse`` / ``check_export_fuse`` 在导出前按文件
  统计未翻译 / 失败 / 标签校验未过 / 匹配率，任一触发即抛 ``ExportBlocked``。

DECISION(§6.2)：兜底链只对「target 不是本引擎产生」的场景生效（迁移 / CSV 手改）；
正常引擎产出的译文直写 uid，不经过此链。匹配不修改入参，调用方（W5）负责把匹配结果
写回 Segment.target 并复跑校验。
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
    "SIMILARITY_THRESHOLD", "normalize_feature", "match_translations",
    "evaluate_export_fuse", "check_export_fuse",
]

# §6.2 兜底相似度阈值（代码常量）
SIMILARITY_THRESHOLD = 0.92

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
    on_warning: Optional[Callable[[str, TranslationRow, Segment], None]] = None,
) -> MatchOutcome:
    """§6.2 兜底链：为每条外来译文匹配一个 Segment。

    匹配优先级（命中即停）：
    1. ``uid`` 精确（主键）；
    2. 特征码归一化后等值；
    3. 定位符相同且 ``SequenceMatcher`` 相似度 ≥ 阈值（命中打 warning，§6.2 末级兜底）；
    4. 三级均失败 → 计入 ``unmatched``。

    一个 Segment 至多被一条译文占用（``consumed`` 记录已匹配 uid），避免重复写回。
    """
    segs = list(segments)
    row_list = list(rows)

    uid_index: dict[int, list[Segment]] = {}
    feat_index: dict[str, list[Segment]] = {}
    loc_index: dict[str, list[Segment]] = {}
    for s in segs:
        uid_index.setdefault(s.uid, []).append(s)
        fn = normalize_feature(s.feature)
        if fn:
            feat_index.setdefault(fn, []).append(s)
        if s.locator:
            loc_index.setdefault(s.locator, []).append(s)

    consumed: set[int] = set()
    matches: list[TranslationMatch] = []
    unmatched: list[TranslationRow] = []

    def take(cands: list[Segment]) -> Optional[Segment]:
        for s in cands:
            if s.uid not in consumed:
                consumed.add(s.uid)
                return s
        return None

    for row in row_list:
        seg: Optional[Segment] = None
        method: Optional[str] = None

        # 1) uid 精确（主键）
        if row.uid is not None and row.uid in uid_index:
            seg = take(uid_index[row.uid])
            if seg is not None:
                method = "uid"

        # 2) 特征码归一化等值
        if seg is None and row.feature:
            cand = feat_index.get(normalize_feature(row.feature))
            if cand:
                seg = take(cand)
                if seg is not None:
                    method = "feature"

        # 3) 相似度兜底（同定位符 + 相似度≥阈值）
        if seg is None and row.locator and row.locator in loc_index:
            best: Optional[Segment] = None
            best_ratio = 0.0
            for s in loc_index[row.locator]:
                if s.uid in consumed:
                    continue
                ratio = difflib.SequenceMatcher(
                    None, row.source or "", s.source or ""
                ).ratio()
                if ratio >= similarity_threshold and ratio > best_ratio:
                    best_ratio = ratio
                    best = s
            if best is not None:
                consumed.add(best.uid)
                seg = best
                method = "similarity"
                logger.warning(
                    "§6.2 相似度兜底命中 uid=%s 相似度=%.3f（定位符=%s）",
                    seg.uid, best_ratio, row.locator,
                )
                if on_warning is not None:
                    on_warning("similarity_match", row, seg)

        if seg is not None:
            matches.append(TranslationMatch(row, seg, method))  # type: ignore[arg-type]
        else:
            unmatched.append(row)

    return MatchOutcome(matches=matches, unmatched=unmatched)


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
