"""UiState 状态机测试（W5 交付物，设计 §9）。

覆盖 §9 三条置灰规则、进度条、步骤切换、审校页导航（j/k、a 通过、f 失败、Ctrl+S）。
UiState 不依赖 Tk，可纯逻辑单测。
"""

from __future__ import annotations

from pytran.models import Project, Segment, SegmentStatus
from pytran.ui_state import (
    REVIEW_FILTERS,
    STEP_CHOOSE_DIR,
    STEP_EXPORT,
    STEP_TRANSLATE,
    UiState,
)


def _make_project(game_root: str = "") -> Project:
    def seg(uid, source, target, status, error=None):
        return Segment(
            uid=uid, file="a.rpy", locator=f"loc{uid}", feature=f"f{uid}",
            kind="dialogue", comment="", role="", source=source, target=target,
            status=status, error=error,
        )
    return Project(
        game_root=game_root,
        segments=[
            seg(1, "Hello", "你好", SegmentStatus.TRANSLATED),
            seg(2, "World", "", SegmentStatus.PENDING),
            seg(3, "x", "", SegmentStatus.FAILED, error="english"),
            seg(4, "keep", "keep", SegmentStatus.KEEP_DECLARED),
        ],
    )


def test_no_game_root_disables_tools() -> None:
    proj = _make_project(game_root="")  # 纯文件模式
    st = UiState(proj)
    assert st.control_enabled("tools_unpack") is False
    assert st.control_enabled("tools_writeback") is False


def test_game_root_enables_tools() -> None:
    proj = _make_project(game_root="C:/game")
    st = UiState(proj)
    assert st.control_enabled("tools_unpack") is True
    assert st.control_enabled("tools_writeback") is True


def test_no_segments_disables_translate() -> None:
    proj = Project(segments=[])
    st = UiState(proj)
    assert st.control_enabled("translate") is False
    assert st.control_enabled("export_writeback") is False


def test_no_translated_disables_export_and_review() -> None:
    proj = Project(segments=[
        Segment(uid=1, file="a.rpy", locator="loc1", feature="f1", kind="dialogue",
                comment="", role="", source="Hi", target="", status=SegmentStatus.PENDING),
    ])
    st = UiState(proj)
    assert st.control_enabled("export_writeback") is False
    assert st.control_enabled("review") is False


def test_has_translated_enables_export_review() -> None:
    proj = _make_project()
    st = UiState(proj)
    assert st.control_enabled("export_writeback") is True
    assert st.control_enabled("review") is True
    assert st.control_enabled("translate") is True


def test_retry_failed_only_when_failed() -> None:
    proj = _make_project()  # uid3 failed
    st = UiState(proj)
    assert st.control_enabled("retry_failed") is True
    # 清掉失败行
    proj.segments[2].status = SegmentStatus.TRANSLATED
    assert st.control_enabled("retry_failed") is False


def test_all_controls_keys_present() -> None:
    st = UiState(_make_project())
    controls = st.all_controls()
    assert set(controls) == {
        "choose_dir", "import_rpy", "translate", "review",
        "export_writeback", "tools_unpack", "tools_writeback", "retry_failed",
    }


def test_progress_counts() -> None:
    st = UiState(_make_project())
    p = st.progress()
    assert p["total"] == 4
    # done 仅统计 translated/reviewed → uid1 仅 translated，uid4 keep 不算
    assert p["done"] == 1
    assert p["failed"] == 1


def test_goto_bounds() -> None:
    st = UiState(_make_project())
    st.goto(STEP_TRANSLATE)
    assert st.current_step == STEP_TRANSLATE
    try:
        st.goto(STEP_EXPORT + 1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_review_filter_navigation() -> None:
    st = UiState(_make_project())
    st.set_filter("failed")
    items = st.review_items()
    assert len(items) == 1
    assert items[0].uid == 3
    # 未知过滤器抛错
    try:
        st.set_filter("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_review_accept_and_flag() -> None:
    st = UiState(_make_project())
    st.set_filter("pending")          # uid2
    cur = st.current_review()
    assert cur.uid == 2
    assert st.accept_current() is True
    assert st.project.segments[1].status == SegmentStatus.REVIEWED
    # 标记失败
    st.set_filter("translated")       # uid1
    assert st.flag_failed_current("manual") is True
    assert st.project.segments[0].status == SegmentStatus.FAILED
    assert st.project.segments[0].error == "manual"


def test_review_save_target() -> None:
    st = UiState(_make_project())
    st.set_filter("pending")
    assert st.save_target("新译文") is True
    assert st.project.segments[1].target == "新译文"
    # 空列表时 current_review 返回 None，save 返回 False
    st.set_filter("reviewed")   # 无 reviewed 行
    assert st.current_review() is None
    assert st.save_target("x") is False


def test_review_move_next_prev_clamped() -> None:
    st = UiState(_make_project())
    st.set_filter("all")
    st.move_prev()                    # 已在 0，不越界
    assert st._review_index == 0
    st.move_next()
    assert st._review_index == 1
    st.move_next()
    st.move_next()
    st.move_next()
    # 夹紧到末尾
    assert st._review_index == 3
    st.move_prev()
    assert st._review_index == 2


def test_review_filters_constant_complete() -> None:
    # W5g §2：新增 keep_original（与 keep_declared 并列，二者都显示「保留原文」）
    assert REVIEW_FILTERS == (
        "all", "pending", "translated", "failed",
        "keep_declared", "keep_original", "reviewed",
    )


def test_keep_original_filter_selects_only_that_status() -> None:
    proj = _make_project()
    proj.segments.append(Segment(
        uid=5, file="a.rpy", locator="loc5", feature="f5", kind="dialogue",
        comment="", role="", source="orig", target=None,
        status=SegmentStatus.KEEP_ORIGINAL,
    ))
    st = UiState(proj)
    st.set_filter("keep_original")
    items = st.review_items()
    assert [i.uid for i in items] == [5]
    # keep_declared（uid4）不应出现在 keep_original 下
    st.set_filter("keep_declared")
    items = st.review_items()
    assert [i.uid for i in items] == [4]


def test_keep_original_current_marks_status_and_target() -> None:
    proj = _make_project()
    proj.segments.append(Segment(
        uid=5, file="a.rpy", locator="loc5", feature="f5", kind="dialogue",
        comment="", role="", source="原文全文", target=None,
        status=SegmentStatus.FAILED, error="english",
    ))
    st = UiState(proj)
    st.set_filter("all")
    st._review_index = 4  # 指向 uid5
    assert st.keep_original_current() is True
    seg = proj.segments[4]
    assert seg.status == SegmentStatus.KEEP_ORIGINAL
    assert seg.target == "原文全文"      # target 被设为原文
    assert seg.error is None            # 清空失败原因


def test_keep_original_current_no_selection_returns_false() -> None:
    proj = _make_project()
    st = UiState(proj)
    st.set_filter("reviewed")  # 无 reviewed 行 → current_review 为 None
    assert st.current_review() is None
    assert st.keep_original_current() is False


def test_navigate_after_status_change_lands_on_next_visible() -> None:
    # W5g 回归：保留原文/通过/失败后游标不应“跳两格”。
    # pending 筛选下，标记 KEEP_ORIGINAL 的条目会从列表消失，其后条目上移一格；
    # 行定位（按 uid 顺序的下一可见条目）应使游标停在紧邻的下一条可见项。
    proj = Project(segments=[
        Segment(uid=1, file="a.rpy", locator="l1", feature="f1", kind="dialogue",
                comment="", role="", source="s1", target="", status=SegmentStatus.PENDING),
        Segment(uid=2, file="a.rpy", locator="l2", feature="f2", kind="dialogue",
                comment="", role="", source="s2", target="", status=SegmentStatus.PENDING),
        Segment(uid=3, file="a.rpy", locator="l3", feature="f3", kind="dialogue",
                comment="", role="", source="s3", target="", status=SegmentStatus.PENDING),
    ])
    st = UiState(proj)
    st.set_filter("pending")
    st._review_index = 1          # 选中 uid2 (s2)
    assert st.current_review().uid == 2
    # 该 uid2 标记 KEEP_ORIGINAL → 从 pending 列表消失
    st.keep_original_current()
    assert proj.segments[1].status == SegmentStatus.KEEP_ORIGINAL
    st.navigate_after_status_change(2)
    items = st.review_items()
    # 游标应停在 uid3 (s3)，即被移除项的紧邻下一条可见项，而非跳过它
    assert st.current_review().uid == 3
    assert [i.uid for i in items] == [1, 3]


def test_navigate_after_status_change_all_filter_advances_one() -> None:
    # all 筛选下，标记后条目仍可见，游标应前进一格（等同于原 +1 行为）
    proj = Project(segments=[
        Segment(uid=1, file="a.rpy", locator="l1", feature="f1", kind="dialogue",
                comment="", role="", source="s1", target="", status=SegmentStatus.PENDING),
        Segment(uid=2, file="a.rpy", locator="l2", feature="f2", kind="dialogue",
                comment="", role="", source="s2", target="", status=SegmentStatus.PENDING),
        Segment(uid=3, file="a.rpy", locator="l3", feature="f3", kind="dialogue",
                comment="", role="", source="s3", target="", status=SegmentStatus.PENDING),
    ])
    st = UiState(proj)
    st.set_filter("all")
    st._review_index = 1
    st.keep_original_current()        # 标记 uid2，仍可见
    st.navigate_after_status_change(2)
    assert st._review_index == 2     # 前进一格
    assert st.current_review().uid == 3


def test_keep_original_not_counted_as_done() -> None:
    proj = _make_project()
    proj.segments.append(Segment(
        uid=5, file="a.rpy", locator="loc5", feature="f5", kind="dialogue",
        comment="", role="", source="orig", target="orig",
        status=SegmentStatus.KEEP_ORIGINAL,
    ))
    st = UiState(proj)
    p = st.progress()
    assert p["total"] == 5
    # done 仅统计 translated/reviewed → keep_original（uid5）与 keep_declared（uid4）均不计
    assert p["done"] == 1
    assert p["failed"] == 1
