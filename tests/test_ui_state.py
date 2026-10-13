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
    assert REVIEW_FILTERS == (
        "all", "pending", "translated", "failed", "keep_declared", "reviewed"
    )
