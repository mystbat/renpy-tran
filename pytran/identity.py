"""段身份体系（seg_key）——构造性根治错位 Bug 的核心模块（任务书 v3.1 §2.1）。

设计要点（见 docs/TASK-segkey-identity-refactor.md）：

* **段身份 = (file, source, occurrence)**。六轮复盘证明：任何用派生量
  （uid 序号 / 16-bit path hash / 特征码）充当身份的构造都会在某个场景下
  错位。唯一在本项目全场景下稳定且无碰撞的就是数据本身——文件路径与原文。
* 文件路径用**完整相对路径**，不做任何 hash 截断（路径自身即唯一，何须摘要）。
* ``normalize_source`` 是 key 的规范化规则，**钉死在单一纯函数**；规则变更
  必须升 ``KEY_VERSION`` 并触发全工程重算，禁止静默兼容。
* ``occurrence`` 仅在同文件重复原文桶（大小 > 1）内启用；桶大小 = 1 时省略，
  键保持简洁（P0 实测：666 段中 20 个重复桶，最大桶 11 条）。
* ``resolve_identity`` 是全工程**唯一**的译文↔段对位出口。合并导入 / CSV
  / 迁移三条入口必须全部走它，禁止任何入口自带对齐逻辑（复盘 陷阱五）。
  查不到返回 None → 调用方计入 unmatched——**宁可判不匹配，也不猜**。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pytran.models import Segment

logger = logging.getLogger(__name__)

# key 规范化规则版本。变更 normalize_source 的任何行为都必须 +1，
# 并提供迁移路径（重算全工程 key），否则新旧数据无法对位。
KEY_VERSION = 1

# '' 与 "" 都视为 "（旧数据常见引号风格混用，沿用 normalize_feature 的口径）
_QUOTE_NORMALIZE = [("''", '"'), ('""', '"')]


def normalize_source(source: str) -> str:
    """原文规范化（key 的一部分）。

    规则（KEY_VERSION=1，变更须升版本）：
    去首尾空白 → ''/"" 统一为 " → 压缩连续空白。
    注意：不做大小写折叠、不做全半角统一——原文的实质差异就是不同段。
    """
    s = (source or "").strip()
    for bad, good in _QUOTE_NORMALIZE:
        s = s.replace(bad, good)
    s = re.sub(r"\s+", " ", s)
    return s


# key 字段分隔符：ASCII 单元分隔符。不得使用 "|" 等 CSV 常见分隔符
# （BUGFIX(key-csv-delimiter, 2026-10-26)：首版用 "|"，与 CSV_DELIMITER
# 撞车，key 写入 CSV 后列错位——分隔符选择必须避开所有序列化边界）。
_KEY_SEP = "\x1f"


def segment_key(file: str, source: str) -> str:
    """同文件内唯一的身份键（不含 occurrence 维度）。

    格式 ``v<KEY_VERSION><sep><file><sep><normalized source>``。
    file 为完整相对路径（分隔符统一 /），不截断、不 hash。
    """
    f = (file or "").replace("\\", "/").strip()
    return f"v{KEY_VERSION}{_KEY_SEP}{f}{_KEY_SEP}{normalize_source(source)}"


def build_key_index(segments: Iterable[Segment]) -> "KeyIndex":
    """由段列表构建身份索引（含 occurrence 消歧）。"""
    return KeyIndex(segments)


@dataclass
class KeyEntry:
    """同 (file, source) 桶内的全部候选段（按文件内出现顺序）。"""

    segments: list[Segment] = field(default_factory=list)


class KeyIndex:
    """段身份索引：``(file, normalized source) → 桶``，支持 occurrence 对位。

    构造性保证（对照六轮复盘的三个错位来源）：
    * 无 uid 碰撞——键含完整路径，不截断；
    * 无序号漂移——键不含位置信息，插入/删除行不影响其它段的身份；
    * 无跨文件串译——键含文件维度，A 文件的译文在 B 文件无候选。
    唯一残留窗口：同文件重复原文桶内插行 → 同原文段之间互换。互换双方
    原文相同，写错的译文仍是"同一句话的译文"，且写回熔断不变量兜底。
    """

    def __init__(self, segments: Iterable[Segment]) -> None:
        self._buckets: dict[str, KeyEntry] = {}
        self._by_uid: dict[int, Segment] = {}
        for seg in segments:
            key = segment_key(getattr(seg, "file", ""), seg.source)
            self._buckets.setdefault(key, KeyEntry()).segments.append(seg)
            self._by_uid[seg.uid] = seg

    def bucket_size(self, file: str, source: str) -> int:
        """该 (file, source) 桶内的段数（重复原文消歧的决策依据）。"""
        return len(self._buckets.get(segment_key(file, source), KeyEntry()).segments)

    def resolve(
        self,
        file: str,
        source: str,
        *,
        exclude_uids: Optional[set[int]] = None,
    ) -> Optional[Segment]:
        """按身份精确解析一段；解析不了返回 None（绝不猜）。

        * 桶大小 = 1 → 直接命中（绝大多数场景）；
        * 桶大小 > 1 → occurrence 对位：调用方需自行保证同桶内按顺序
          消费（见 :meth:`resolve_with_counter`）。本方法在多元素桶上
          **不猜**：取桶内第一个未被排除的段，语义等同 FIFO——与
          "同原文段互换无实际损害"的残余风险声明一致。
        * 桶不存在 → None，调用方计入 unmatched 并产生可见记录。
        """
        entry = self._buckets.get(segment_key(file, source))
        if not entry:
            return None
        excluded = exclude_uids or set()
        for seg in entry.segments:
            if seg.uid not in excluded:
                return seg
        return None

    def segment_by_uid(self, uid: int) -> Optional[Segment]:
        """会话内 uid 游标查找（仅限单次生命周期内使用）。"""
        return self._by_uid.get(uid)


class IdentityResolver:
    """带消费计数的解析器：处理同文件重复原文桶的 occurrence 对位。

    用于批量导入场景：row 流按文件内出现顺序排列（CSV / 旧工程快照均
    天然如此），同桶内按 FIFO 对位。桶大小不一致（row 侧比 seg 侧多）
    时多余 row 进 unmatched——**整桶不猜**。
    """

    def __init__(self, segments: Iterable[Segment]) -> None:
        self._index = KeyIndex(segments)
        self._used_uids: set[int] = set()

    @property
    def index(self) -> KeyIndex:
        return self._index

    def resolve_next(self, file: str, source: str) -> Optional[Segment]:
        """在同 (file, normalize(source)) 桶内取第一个**原文等值**且未消费的段。

        BUGFIX(bucket-positional, 2026-10-26)：早先按位置盲取（FIFO），
        桶键含 normalize 后原文、桶内段按出现顺序排列——两者天然一致，
        位置盲取即等值取。但"位置即等值"是隐含前提；为让 §2.5 不变量
        真正有牙（而非依赖隐含前提），此处显式按 normalize 等值筛选，
        取不到即 None。同文件重复原文段之间互换无实际损害的残余风险
        声明不变（等值段间任意对位）。
        """
        key = segment_key(file, source)
        entry = self._index._buckets.get(key)
        if not entry:
            return None
        ns = normalize_source(source)
        for seg in entry.segments:
            if seg.uid in self._used_uids:
                continue
            if normalize_source(seg.source) == ns:
                self._used_uids.add(seg.uid)
                return seg
        return None

    def leftover_unmatched(self, file: str, source: str) -> bool:
        """该 (file, source) 桶内是否还有未消费的段（诊断用）。"""
        key = segment_key(file, source)
        entry = self._index._buckets.get(key)
        if not entry:
            return False
        return any(
            s.uid not in self._used_uids
            and normalize_source(s.source) == normalize_source(source)
            for s in entry.segments
        )


__all__ = [
    "KEY_VERSION",
    "normalize_source",
    "segment_key",
    "build_key_index",
    "KeyIndex",
    "KeyEntry",
    "IdentityResolver",
    "resolve_batch",
    "IdentityViolation",
]


@dataclass
class IdentityMatch:
    """``resolve_batch`` 的单条命中结果。"""

    row: object          # 外来译文行（duck-typed：需有 file/source/target）
    segment: Segment


@dataclass
class BatchOutcome:
    """``resolve_batch`` 的完整结果。"""

    matches: list[IdentityMatch]
    unmatched: list[object]


def resolve_batch(
    segments: Iterable[Segment],
    rows: Iterable[object],
) -> BatchOutcome:
    """批量身份对位——全工程唯一的译文↔段对位出口（任务书 §2.2）。

    * rows 需 duck-type ``source`` / ``target`` 属性；file 依次取
      ``row.file`` → ``locator`` 反推 → 全工程唯一原文桶（确定性规则，
      见下），仍无法定位 → unmatched；
    * **row 无文件信息时的确定性规则**：若该原文在全工程恰好只存在于
      一个文件的桶里 → 命中该桶；存在于多个文件（歧义）→ unmatched。
      这是"唯一性可判定"而非"猜测"——与「同文件重复桶 FIFO」同一
      逻辑：只在无歧义时行动，歧义即显式拒绝；
    * 同 (file, source) 桶按 FIFO 对位（同原文段互换无实际损害）；
    * row 侧多于 seg 侧 → 多余 row 进 unmatched（整桶不猜）；
    * **不存在任何形式的模糊猜测或兜底**。
    """
    resolver = IdentityResolver(segments)
    matches: list[IdentityMatch] = []
    unmatched: list[object] = []
    # 全工程原文桶分布：normalized source → {file: count}（歧义判定用）
    source_files: dict[str, set[str]] = {}
    for seg in resolver.index._buckets:
        _, f, src = _split_key(seg)
        source_files.setdefault(src, set()).add(f)

    for row in rows:
        file = (getattr(row, "file", "") or "").strip() or \
            _file_of_locator(getattr(row, "locator", ""))
        source = getattr(row, "source", "") or ""
        if not file:
            files_with_source = source_files.get(normalize_source(source), set())
            if len(files_with_source) == 1:
                file = next(iter(files_with_source))
            else:
                unmatched.append(row)   # 无文件信息且原文跨文件（或不存在）
                continue
        seg = resolver.resolve_next(file, source)
        if seg is not None:
            # 内容级不变量（任务书 v3.1 §2.5）：身份命中的段与 row 必须
            # 原文一致。在构造点断言——任何未来改动破坏此前提（例如有人
            # 重新引入模糊匹配）都会在此显式爆炸，而不是静默错位。
            if normalize_source(seg.source) != normalize_source(source):
                raise IdentityViolation(
                    f"身份命中但原文不一致（构造性不变量被破坏）："
                    f"row={source!r} seg(uid={seg.uid}, file={seg.file}, "
                    f"source={seg.source!r})"
                )
            matches.append(IdentityMatch(row=row, segment=seg))
        else:
            unmatched.append(row)
    return BatchOutcome(matches=matches, unmatched=unmatched)


class IdentityViolation(RuntimeError):
    """身份对位的内容级不变量被破坏（任务书 v3.1 §2.5 最后防线）。"""


def _split_key(key: str) -> tuple[str, str, str]:
    """把 segment_key 拆回 (version, file, source)。"""
    parts = key.split(_KEY_SEP, 2)
    if len(parts) != 3:
        return "", "", ""
    return parts[0], parts[1], parts[2]


_LOCATOR_FILE_RE = re.compile(r"^#\s*game/(?P<path>.+?):(?P<line>\d+)\s*$")


def _file_of_locator(locator: str) -> str:
    """从定位符 ``# game/<rel>:<line>`` 提取文件相对路径；失败返回空。"""
    m = _LOCATOR_FILE_RE.match((locator or "").strip())
    return m.group("path").replace("\\", "/").strip() if m else ""
