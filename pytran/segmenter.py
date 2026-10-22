"""切块（chunking）：按 uid 顺序、动态块大小、带上下文（设计 §2.2、§4.1）。

W1 只负责**切块与上下文编排**，不含翻译逻辑（翻译在 W4）。切块目标：
引擎把每个 Chunk 作为一个请求单元发给 provider（设计 §4.1）。

* 顺序：严格按 Segment.uid 升序（设计 §3.1：uid 全局唯一、只增不减，
  切块/重排不破坏对齐）。
* 块大小：默认 `max_lines`（软上限，设计 §4.1 "每 30 行"）。**动态**体现在
  块大小可由调用方按 token 预算调整（W4 再接 token 估算），W1 提供固定软
  上限 + 可配置接口，预留动态钩子。
* 上下文：每个块携带**前一块已成功的最后 `context_lines` 行**（原文+译文对，
  设计 §4.1 要点、§4.1 user 示例 "前文参考"）。第一块无上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Segment, SegmentStatus


@dataclass
class Chunk:
    """一个翻译请求单元（设计 §4.1）。"""

    index: int
    segments: list[Segment] = field(default_factory=list)
    # (source, target) 对，来自上一个块末尾已成功翻译的行（设计 §4.1）。
    context: list[tuple[str, str]] = field(default_factory=list)

    def lines(self) -> list[tuple[int, str]]:
        """返回 (uid, source) 列表，供协议层拼请求。"""
        return [(s.uid, s.source) for s in self.segments]

    def translatable_count(self) -> int:
        return len(self.segments)


class Segmenter:
    """把 Segment 列表切成有序、带上下文的 Chunk 列表（设计 §2.2）。"""

    def __init__(
        self,
        max_lines: int = 30,
        context_lines: int = 3,
        include_statuses: Optional[frozenset[SegmentStatus]] = None,
    ) -> None:
        # DECISION(§4.1): 默认软上限 30 行；context 取前块末尾 3 行。
        self.max_lines = max(1, max_lines)
        self.context_lines = max(0, context_lines)
        # 只切块"可翻译"的段；skipped 段不进翻译队列（设计 §14：原样保留）。
        # W5g §2：KEEP_DECLARED / KEEP_ORIGINAL 不进队列（与 engine._build_queue、
        # ui._translatable_uids 同一口径），故默认仅 PENDING / FAILED。
        self.include_statuses = include_statuses or frozenset(
            {
                SegmentStatus.PENDING,
                SegmentStatus.FAILED,
            }
        )

    def chunk(self, segments: list[Segment]) -> list[Chunk]:
        """切块主入口。返回按 uid 升序、带上下文的 Chunk 列表。"""
        work = [s for s in segments if s.status in self.include_statuses]
        work.sort(key=lambda s: s.uid)
        # 已成功翻译的段（用于上下文），按 uid 升序，供按"块首 uid 之前"取末尾用。
        translated = sorted(
            (s for s in segments if s.target),
            key=lambda s: s.uid,
        )
        chunks: list[Chunk] = []
        for start in range(0, len(work), self.max_lines):
            group = work[start : start + self.max_lines]
            context = self._context_from(translated, group)
            chunks.append(
                Chunk(index=len(chunks), segments=group, context=context)
            )
        return chunks

    def _context_from(
        self, translated: list[Segment], group: list[Segment]
    ) -> list[tuple[str, str]]:
        # DECISION(§4.1): 上下文 = 当前块之前、已成功翻译的最后 context_lines 行
        # （原文+译文对）。已翻译段本身不进翻译队列，但为后续块提供衔接。
        if not translated or self.context_lines == 0 or not group:
            return []
        first_uid = group[0].uid
        pairs = [
            (s.source, s.target)
            for s in translated
            if s.uid < first_uid and s.target
        ]
        return pairs[-self.context_lines :]


__all__ = ["Segmenter", "Chunk"]
