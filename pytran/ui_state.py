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

import re
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
# W5g §2：新增 keep_original（人工保留原文），与 keep_declared 并列，
# 但二者在 REVIEW_FILTER_LABELS 均显示「保留原文」（按"不区分"处理）。
REVIEW_FILTERS = ("all", "pending", "translated", "failed",
                  "keep_declared", "keep_original", "reviewed")


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

    def keep_original_current(self) -> bool:
        """W5g §2.2「k 保留原文」：当前行标记 KEEP_ORIGINAL，
        并把 target 设为原文（保留原文全文，写回时用 source 残片）。
        不计入「已翻译/已审校」完成数（progress 口径不变）。"""
        seg = self._current_segment()
        if seg is None:
            return False
        seg.status = SegmentStatus.KEEP_ORIGINAL
        seg.target = seg.source
        seg.error = None
        return True

    def navigate_after_status_change(self, changed_uid: int) -> None:
        """标记/改变当前条目状态后，把游标锚定到『该条目的下一条可见条目』。

        修复 W5g 回归（保留原文/通过/失败 后游标“跳两格”）：

        改变状态后若新状态被当前筛选排除（例如 pending 筛选下标记
        KEEP_ORIGINAL → 该条从列表消失），剩余条目整体上移一格，
        旧的 ``_review_index`` 数值不变却已指向紧邻的下一条；若再
        ``_navigate_list(+1)`` 就会再跳一格，视觉上变成“跳两格/跳过
        一条”。本方法按 uid 在 project.segments 顺序中定位 changed_uid
        之后的第一条仍可见条目，使位移恒为 1（在 all 筛选下等价于
        原来的 +1，在被排除筛序下也不再跳过）。
        """
        items = self.review_items()
        if not items:
            self._review_index = 0
            return
        visible_uids = {it.uid for it in items}
        order = list(self.project.segments)
        try:
            pos = next(i for i, s in enumerate(order) if s.uid == changed_uid)
        except StopIteration:
            pos = -1
        # 在被改变条目的下一条开始，找第一个在当前筛选中仍可见的 uid
        target_uid = None
        for s in order[pos + 1:]:
            if s.uid in visible_uids:
                target_uid = s.uid
                break
        if target_uid is not None:
            for i, it in enumerate(items):
                if it.uid == target_uid:
                    self._review_index = i
                    return
        # 其后无可见条目：停在当前（已是最末）可见条
        self._review_index = len(items) - 1

    # ------------------------------------------------------------------
    # W5h 全面审校（只读检查 + 分类结果；不改任何数据）
    # ------------------------------------------------------------------
    def full_audit(self, profile) -> list[dict]:
        """对全部已导入段执行 V1–V6 + 术语未生效 检查，返回分类问题列表。

        纯函数式（不碰 Qt / 文件系统），仅读取 ``project.segments``，
        方便单测。调用方（ui.py）负责把结果渲染成面板并支持双击定位——
        本方法不修改任何段落数据，也不切换游标。

        返回每一条 *有问题* 的段一个 dict::

            {
              "uid": int, "file": str, "locator": str,
              "source": str, "target": str, "status": str,   # SegmentStatus.name
              "checks": list[tuple[str, str]],                # (分类, 详情)
            }

        分类取值（与任务书 §2 五类检查一致）：

        * ``"标签丢失/改动"`` —— ``{...}`` / ``[...]`` 标签结构在译文中丢失或改动；
        * ``"转义丢失/改动"`` —— ``\\n`` 等转义在译文中丢失或改动；
        * ``"英文引号未修复"`` —— 原始译文仍含 ASCII 双引号 ``"``
          （V4 的 ``repair_quotes`` 会把 ``"`` 整体转成「」；若译文里
          还留着 ``"``，说明这批译文未经/逃过自动修复，属于复查要抓的隐患）；
        * ``"英文残余"`` —— 剔除放行项后仍残留英文实词；
        * ``"译文为空"`` —— 译文为空（status 已标记但 target 为空）；
        * ``"术语未生效"`` —— 原文含术语 key（整词），译文却不含对应译名。

        注意：仅检查 *有译文* 的段；PENDING 且无 target 的段直接跳过
        （任务书 §5 用例 10）。

        变更（2026-10-24，用户要求）：本方法**不再做任何拟声词/编号错配
        检测**（原 V7：同译文多原文 / 相邻长度倒挂 / 尖叫行交叉 /
        极端长度兜底）。理由：那些判据是启发式推断，既不能保证召回，
        也会误报，用户明确不接受。错配改由「AI 校对」按钮（模型逐行
        对照原文/译文）负责，见 ``ui.py::_build_audit_dialog``。
        """
        from pytran.validators import (
            Validator, TAG_RE, VAR_RE, ESC_RE,
        )

        try:
            terms = dict(profile.terms) if profile is not None else {}
        except Exception:  # noqa: BLE001 - profile 异常时退化为空术语表
            terms = {}
        validator = Validator()

        issues: list[dict] = []
        for s in self.project.segments:
            target = s.target or ""
            # PENDING 且无译文：跳过（全面审校只复核“已翻出来”的内容，
            # 任务书 §5 用例 10）。其余状态（含 REVIEWED 等）即便 target 为空
            # 也属真实异常，交由 V2「译文为空」上报。
            if not target.strip() and s.status == SegmentStatus.PENDING:
                continue

            checks: list[tuple[str, str]] = []

            # —— V1–V6：复用 Validator.check_detailed ——
            res = validator.check_detailed(s.source, target, profile)

            for err in res.errors:
                if err.startswith("tags:"):
                    # 解析出被改动的 token，区分“标签”还是“转义”
                    token = err.split(":", 1)[1].split()[0] if ":" in err else ""
                    if ESC_RE.fullmatch(token):
                        cat = "转义丢失/改动"
                    elif TAG_RE.fullmatch(token) or VAR_RE.fullmatch(token):
                        cat = "标签丢失/改动"
                    else:
                        cat = "结构错误"
                    checks.append((cat, err))
                elif err == "english":
                    residual = ", ".join(res.residual) if res.residual else "英文片段"
                    checks.append(("英文残余", f"英文残余：{residual}"))
                elif err == "empty":
                    checks.append(("译文为空", "译文为空"))

            # —— 英文双引号未修复（任务书 §2 引号类）——
            # 看“原始译文”是否仍含 ASCII 双引号；V4 修复后不会出现，
            # 留着即代表此段漏过引号处理。
            if '"' in target:
                checks.append(("英文引号未修复", "译文含未修复的英文双引号（\"）"))

            # # DECISION(§7 术语未生效) —— 任务书新增逻辑，不在 validators 内
            for key, val in terms.items():
                key_s, val_s = str(key), str(val)
                if _term_uneffective(s.source, target, key_s, val_s):
                    checks.append(
                        ("术语未生效",
                         f"术语『{key_s}』未生效 → 应为『{val_s}』"))

            if checks:
                status_name = s.status.name if hasattr(s.status, "name") else str(s.status)
                issues.append({
                    "uid": s.uid,
                    "file": s.file,
                    "locator": s.locator,
                    "source": s.source,
                    "target": target,
                    "status": status_name,
                    "checks": checks,
                })
        # 变更（2026-10-24）：此处原有 V7 批次级三类指纹复检，已按用户要求
        # 整段移除（启发式推断不可靠，改由「AI 校对」按钮负责）。
        return issues


def _term_uneffective(source: str, target: str, key: str, val: str) -> bool:
    """术语未生效判定（任务书 §7 新增逻辑）。

    原文 ``source`` 含术语 key（整词优先，CJK/无边界时退化为子串），
    且译文 ``target`` 不含对应译名 ``val`` → 判定未生效。
    """
    if not key:
        return False
    # 整词边界：仅当 key 前后不是 ASCII 字母/数字/下划线时才算命中，
    # 避免 "Ian" 误中 "Julian"。CJK 前后本就非 alnum，自然整词命中。
    pat = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])")
    if not pat.search(source):
        return False
    if val and val in target:
        return False
    return True


def _filter_segments(segments: Iterable[Segment], filt: str) -> list[Segment]:
    if filt == "all":
        return list(segments)
    mapping = {
        "pending": SegmentStatus.PENDING,
        "translated": SegmentStatus.TRANSLATED,
        "failed": SegmentStatus.FAILED,
        "keep_declared": SegmentStatus.KEEP_DECLARED,
        "keep_original": SegmentStatus.KEEP_ORIGINAL,
        "reviewed": SegmentStatus.REVIEWED,
    }
    want = mapping.get(filt)
    if want is None:
        return list(segments)
    return [s for s in segments if s.status == want]
