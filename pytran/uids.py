"""uid 分配器（v3.1 降级版）：uid 是**会话内**段序号，仅作单次
「解析→翻译→写回」生命周期内的主键（engine seg_map / 预览回查 /
审校游标 / 提示词编号）。

DECISION(segkey-identity, 2026-10-26 / 任务书 v3.1 §2.4)：
六轮复盘证明 uid（顺序号或路径 hash）都不能充当**跨导入**身份——
身份由 ``pytran.identity`` 的 (file, source) 段键承担。uid 因此降级：

* 分配：按解析顺序 ``1..N``（文档顺序，segmenter 批次随之回到真实
  阅读顺序，上下文参考更连贯——顺带收益）；
* 不持久化语义、不跨导入比较、不进导出物判定；
* 旧「路径 hash + 段序号」格式（uid ≥ 1<<16）加载时一次性重编，
  无兼容分支（见 project._migrate_legacy_uids）。

历史教训（保留勿删）：``b13ce9e`` 曾声称「不同文件 hash 不同永不撞车」，
实际 16-bit blake2b 在 3000 个路径上实测 59 次碰撞——任何"永不"断言
写入文档前必须先测量（复盘 陷阱二/四）。
"""
from __future__ import annotations

from typing import Iterable

# 兼容旧调用方 import（path_hash/make_uid 已无实现，见 _no_legacy）


def assign_uids(file_segments: dict[str, list]) -> None:
    """给每个文件的段列表分配会话内序号（原地修改 seg.uid）。

    ``file_segments``: {文件相对路径: [Segment, ...]}（按文件内顺序）。
    跨文件按传入文件的顺序连续编号（保持全局唯一）。
    """
    n = 0
    for _rel_path, segs in file_segments.items():
        for seg in segs:
            n += 1
            seg.uid = n


def assign_uids_flat(segments: Iterable) -> None:
    """便捷版：输入 (文件路径, Segment) 扁平流，按出现顺序编 1..N。"""
    for i, seg in enumerate(segments, start=1):
        seg.uid = i


def path_hash(rel_path: str) -> int:
    """已废弃。v3.1 起身份不含路径 hash；保留函数签名供旧测试过渡，
    返回 0（调用方不应再依赖其取值）。"""
    return 0


def make_uid(rel_path: str, seq: int) -> int:
    """已废弃。返回 seq 本身（会话内序号语义）。"""
    return seq


__all__ = ["assign_uids", "assign_uids_flat", "path_hash", "make_uid"]
