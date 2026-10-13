"""GUI 状态机（设计文档 §9，W5 交付物）。

职责边界（硬规则）：
* 本模块**不依赖 Tk**（也不 import tkinter），全部逻辑可单测；
* 只持有「界面该显示什么 / 哪些控件该置灰 / 审校页游标在哪」这类状态；
* 真正的翻译 / 解包 / 写回等副作用由调用方（ui.py / cli.py）通过委托注入，
  本模块不 import 引擎、不碰文件系统；
* §9 的「状态驱动置灰」规则在此集中实现，保证 GUI 视图层与状态层一致。

§9 三条置灰规则：
1. 无游戏目录（game_root 为空，纯文件模式）时「解包 / 写回」灰；
2. 无 segments 时「翻译」灰；
3. 无 translated（含 reviewed）时「导出并写回」灰。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from pytran.models import Project, Segment, SegmentStatus

__all__ = [
    "STEP_CHOOSE_DIR", "STEP_IMPORT", "STEP_TRANSLATE", "STEP_REVIEW",
    "STEP_EXPORT", "REVIEW_FILTERS", "ReviewItem", "UiState",
]

# §9 顶部五步（0..4）
STEP_CHOOSE_DIR = 0   # 选择游戏目录
STEP_IMPORT = 1       # 导入 rpy
STEP_TRANSLATE = 2    # 翻译
STEP_REVIEW = 3       # 审校
STEP_EXPORT = 4       # 导出并写回

# 审校过滤器（§9 审校页：左右对照 + 状态筛选）
REVIEW_FILTERS = ("all", "pending", "translated", "failed", "keep_declared", "reviewed")


@dataclass
class ReviewItem:
    """审校页一行（只读快照）。"""

    uid: int
    file: str
    source: str
    target: Optional[str]
    status: SegmentStatus
    error: Optional[str]


class UiState:
    """单窗口状态机（§9）。

    不持有 Tk 对象；通过 ``control_enabled`` 暴露置灰决策，通过
    ``review_items`` / 游标方法暴露审校页导航。单条 accept/fail 直接改
    ``project.segments`` 的状态（轻量，快照与撤销由 exporter.bulk_accept 负责）。
    """

    def __init__(self, project: Project) -> None:
        self.project = project
        self.current_step: int = STEP_CHOOSE_DIR
        self._review_index: int = 0
        self._review_filter: str = "all"

    # -- §9 三条置灰规则基础判定 --------------------------------
    @property
    def has_game_root(self) -> bool:
        """纯文件模式（拖拽 rpy）game_root 为空 → 工具区不可用。"""
        return bool(self.project.game_root)

    @property
    def has_segments(self) -> bool:
        return len(self.project.segments) > 0

    @property
    def has_translated(self) -> bool:
        return any(
            s.status in (SegmentStatus.TRANSLATED, SegmentStatus.REVIEWED)
            for s in self.project.segments
        )

    # -- 控件置灰决策（§9） --------------------------------------
    def control_enabled(self, name: str) -> bool:
        """返回某控件是否可用。

        名称集合（对应 §9 顶部条 + 工具区）：
        choose_dir / import_rpy / translate / review /
        export_writeback / tools_unpack / tools_writeback / retry_failed
        """
        if name in ("tools_unpack", "tools_writeback"):
            # 规则 1：无游戏目录时工具区灰
            return self.has_game_root
        if name == "translate":
            # 规则 2：无 segments 时翻译灰
            return self.has_segments
        if name in ("export_writeback", "review"):
            # 规则 3：无 translated 时导出 / 审校灰
            return self.has_translated
        if name == "import_rpy":
            # 导入：游戏模式或纯文件模式都可（落 rpy 即可）
            return True
        if name == "choose_dir":
            return True
        if name == "retry_failed":
            return any(s.status == SegmentStatus.FAILED for s in self.project.segments)
        raise KeyError(f"unknown control: {name!r}")

    def all_controls(self) -> dict[str, bool]:
        """一次性给出全部控件可用态（GUI 初始化/刷新用）。"""
        return {n: self.control_enabled(n) for n in (
            "choose_dir", "import_rpy", "translate", "review",
            "export_writeback", "tools_unpack", "tools_writeback", "retry_failed",
        )}

    # -- 进度条（§9：done/total/failed） -------------------------
    def progress(self) -> dict[str, int]:
        total = len(self.project.segments)
        done = sum(
            1 for s in self.project.segments
            if s.status in (SegmentStatus.TRANSLATED, SegmentStatus.REVIEWED)
        )
        failed = sum(1 for s in self.project.segments if s.status == SegmentStatus.FAILED)
        return {"done": done, "total": total, "failed": failed}

    # -- 步骤切换（顶部条点击） ----------------------------------
    def goto(self, step: int) -> None:
        if not STEP_CHOOSE_DIR <= step <= STEP_EXPORT:
            raise ValueError(f"step out of range: {step}")
        self.current_step = step

    # -- 审校页导航（§9：j/k 上下，a 通过，f 标记失败） ----------
    def set_filter(self, filt: str) -> None:
        if filt not in REVIEW_FILTERS:
            raise KeyError(f"unknown review filter: {filt!r}")
        self._review_filter = filt
        self._review_index = 0

    @property
    def review_filter(self) -> str:
        return self._review_filter

    def review_items(self) -> list[ReviewItem]:
        """按当前过滤器返回审校列表（§9 审校页左右对照数据源）。"""
        visible = _filter_segments(self.project.segments, self._review_filter)

        def to_item(s: Segment) -> ReviewItem:
            return ReviewItem(
                uid=s.uid, file=s.file, source=s.source, target=s.target,
                status=s.status, error=s.error,
            )

        return [to_item(s) for s in visible]

    def current_review(self) -> Optional[ReviewItem]:
        items = self.review_items()
        if not items:
            return None
        idx = min(self._review_index, len(items) - 1)
        return items[idx]

    def move_next(self) -> None:
        items = self.review_items()
        if items:
            self._review_index = min(self._review_index + 1, len(items) - 1)

    def move_prev(self) -> None:
        if self._review_index > 0:
            self._review_index -= 1

    def _current_segment(self) -> Optional[Segment]:
        cur = self.current_review()
        if cur is None:
            return None
        for s in self.project.segments:
            if s.uid == cur.uid:
                return s
        return None

    def accept_current(self) -> bool:
        """§9 「a 通过」：当前行置 REVIEWED（自动重译不再覆盖，§6.5）。"""
        seg = self._current_segment()
        if seg is None:
            return False
        seg.status = SegmentStatus.REVIEWED
        seg.error = None
        return True

    def flag_failed_current(self, reason: str = "manual") -> bool:
        """§9 「f 标记失败」。"""
        seg = self._current_segment()
        if seg is None:
            return False
        seg.status = SegmentStatus.FAILED
        seg.error = reason
        return True

    def save_target(self, target: str) -> bool:
        """Ctrl+S 保存当前行译文（仅改 target，不改状态）。"""
        seg = self._current_segment()
        if seg is None:
            return False
        seg.target = target
        return True


def _filter_segments(segments: Iterable[Segment], filt: str) -> list[Segment]:
    if filt == "all":
        return list(segments)
    mapping = {
        "pending": SegmentStatus.PENDING,
        "translated": SegmentStatus.TRANSLATED,
        "failed": SegmentStatus.FAILED,
        "keep_declared": SegmentStatus.KEEP_DECLARED,
        "reviewed": SegmentStatus.REVIEWED,
    }
    want = mapping.get(filt)
    if want is None:
        return list(segments)
    return [s for s in segments if s.status == want]
