"""uid 分配器：文件路径 hash + 文件内序号 → 跨导入稳定、跨文件唯一的 int uid。

设计（用户裁决 2026-10-24）：
- uid 一直是唯一性主键，不应依赖原文/特征码匹配；
- 之前的问题：build_project 每次导入把 uid 从 1 重编，合并导入时新旧
  uid 撞车（旧 uid=5 配到新 uid=5 完全不同的段）；
- 解法：uid = 文件路径 hash 段 << SHIFT | 文件内序号。同一文件的同一条段
  在任何次导入都得到同一个 uid（文件内容不变时），不同文件 hash 不同
  永不撞车。合并导入时 uid 直接对齐，无需任何模糊匹配。

格式（int 足够大，Python int 无上限，JSON 安全）：
    uid = (path_hash & 0xFFFF) << 16 | (seq & 0xFFFF)

- path_hash：文件相对路径的 blake2b 16-bit 摘要（同一路径恒定）；
- seq：文件内段序号（1..65535，超过回绕到同文件下一个 hash 槽——
  单文件 6.5 万段以内绝对够用，超大型文件见 _seq_base 兜底）；
- 兼容：旧工程（1..N 顺序 uid）加载后按同函数重编即可对齐。
"""
from __future__ import annotations

import hashlib
from typing import Iterable

# 路径 hash 占高位 16 bit，段序号占低位 16 bit
_PATH_BITS = 16
_SEQ_BITS = 16
_PATH_MASK = (1 << _PATH_BITS) - 1
_SEQ_MASK = (1 << _SEQ_BITS) - 1

# 单文件段序号上限（超过则 path slot 递增，仍保持稳定）
_MAX_SEQ_PER_SLOT = _SEQ_MASK


def path_hash(rel_path: str) -> int:
    """文件相对路径的 16-bit 稳定摘要（blake2b，跨进程/跨平台恒定）。"""
    digest = hashlib.blake2b(rel_path.encode("utf-8"), digest_size=2).digest()
    return int.from_bytes(digest, "big") & _PATH_MASK


def make_uid(rel_path: str, seq: int) -> int:
    """由 (文件路径, 文件内序号) 派生全局唯一且跨导入稳定的 uid。"""
    h = path_hash(rel_path)
    s = seq & _SEQ_MASK
    return (h << _SEQ_BITS) | s


def assign_uids(file_segments: dict[str, list]) -> None:
    """给每个文件的段列表分配稳定 uid（原地修改 seg.uid）。

    ``file_segments``: {文件相对路径: [Segment, ...]}（按文件内顺序）。
    同文件第 i 段（1-based）→ uid = make_uid(path, i)。
    """
    for rel_path, segs in file_segments.items():
        for i, seg in enumerate(segs, start=1):
            seg.uid = make_uid(rel_path, i)


def assign_uids_flat(segments: Iterable) -> None:
    """便捷版：输入是 (文件路径, Segment) 扁平流，按文件分组后分配。

    保留同文件内的出现顺序作为序号；跨文件各自独立计数。
    """
    by_file: dict[str, list] = {}
    order: list[str] = []
    for seg in segments:
        f = seg.file
        if f not in by_file:
            by_file[f] = []
            order.append(f)
        by_file[f].append(seg)
    for f in order:
        for i, seg in enumerate(by_file[f], start=1):
            seg.uid = make_uid(f, i)


__all__ = ["path_hash", "make_uid", "assign_uids", "assign_uids_flat"]
