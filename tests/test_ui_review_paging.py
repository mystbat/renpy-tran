"""审校页筛选与分页的无头回归测试（UI 重构交付物）。

背景：真实工程可达十万级段落（实测 135,507 段）。早期实现会在启动时
一次性创建全部 Qt 列表条目（实测 11.1 秒假死），且界面没有任何筛选控件，
提示文案却让用户"用筛选器缩小范围"。

本模块锁定修复后的行为：

* 列表单次渲染不超过 ``REVIEW_PAGE_SIZE`` 条，尾部提示行不可选中；
* 「显示更多」按批追加，且不会污染审校游标；
* 筛选下拉的选项严格来自 ``UiState.REVIEW_FILTERS``，切换后行数与状态一致；
* 筛选后条目变少时，游标被收回到已渲染范围内（不会选中提示行）。

PyQt6 属于可选的 GUI 依赖：未安装时整体跳过，保证纯文件环境仍可跑测试。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="审校页测试需要 PyQt6（GUI 依赖，可选）")

# 无显示环境（CI / 沙箱）下用 offscreen 平台，必须在 QApplication 之前设置。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import REVIEW_PAGE_SIZE, PytranApp  # noqa: E402
from pytran.ui_state import REVIEW_FILTERS  # noqa: E402

# 造的段落数要跨过一页，才能验证分页与游标收敛
TOTAL = REVIEW_PAGE_SIZE + 500
N_FAILED = 7
N_TRANSLATED = 11
# 「全部」筛选下的总条目数
ALL_ITEMS = TOTAL + N_FAILED + N_TRANSLATED


def _seg(uid: int, status: SegmentStatus) -> Segment:
    return Segment(
        uid=uid, file="a.rpy", locator=f"loc{uid}", feature=f"f{uid}",
        kind="dialogue", comment="", role="", source=f"line {uid}",
        target="" if status is SegmentStatus.PENDING else f"译 {uid}",
        status=status,
    )


def _make_project() -> Project:
    segments = [_seg(i, SegmentStatus.PENDING) for i in range(1, TOTAL + 1)]
    for i in range(TOTAL + 1, TOTAL + 1 + N_FAILED):
        segments.append(_seg(i, SegmentStatus.FAILED))
    for i in range(TOTAL + 1 + N_FAILED, TOTAL + 1 + N_FAILED + N_TRANSLATED):
        segments.append(_seg(i, SegmentStatus.TRANSLATED))
    return Project(game_root="C:/game", segments=segments)


@pytest.fixture(scope="module")
def qt_app():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover - 平台不支持时跳过
        pytest.skip(f"无法创建 QApplication：{exc}")
    return app


@pytest.fixture()
def gui(qt_app, monkeypatch):
    """构建真实窗口（不进入事件循环），返回 PytranApp。"""
    # run() 末尾会阻塞在 app.exec()；替换为立即返回来只验证构建与刷新。
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    app = PytranApp(_make_project(), work_dir=".")
    app._log = lambda *a, **k: None  # 静默日志，保持测试输出干净
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _goto_review(app: PytranApp) -> None:
    app._on_step(3)


def test_review_list_is_capped_and_hint_not_selectable(gui: PytranApp) -> None:
    _goto_review(gui)
    lb = gui._widgets["listbox"]
    # 单页条目 + 1 行不可选中的尾部提示
    assert lb.count() == REVIEW_PAGE_SIZE + 1
    assert gui._displayed_review_rows() == REVIEW_PAGE_SIZE

    hint = lb.item(lb.count() - 1)
    assert not (hint.flags() & Qt.ItemFlag.ItemIsSelectable)


def test_load_more_appends_one_page(gui: PytranApp) -> None:
    _goto_review(gui)
    lb = gui._widgets["listbox"]
    assert lb.count() == REVIEW_PAGE_SIZE + 1

    gui._on_load_more()
    # 总量（1518）落进第二个批次后即全部渲染完毕：不再需要尾部提示行
    assert gui._review_display_cap == 2 * REVIEW_PAGE_SIZE
    assert lb.count() == ALL_ITEMS
    assert gui._displayed_review_rows() == ALL_ITEMS
    assert not gui._widgets["btn_load_more"].isEnabled()


def test_filter_combo_matches_ui_state_filters(gui: PytranApp) -> None:
    combo = gui._widgets["review_filter"]
    assert [combo.itemData(i) for i in range(combo.count())] == list(REVIEW_FILTERS)
    assert combo.currentData() == "all"


def test_filter_switches_row_count(gui: PytranApp) -> None:
    _goto_review(gui)
    combo = gui._widgets["review_filter"]
    lb = gui._widgets["listbox"]

    def select(key: str) -> None:
        combo.setCurrentIndex([combo.itemData(i) for i in range(combo.count())].index(key))

    select("failed")
    assert gui.state.review_filter == "failed"
    assert gui._displayed_review_rows() == N_FAILED

    select("translated")
    assert gui._displayed_review_rows() == N_TRANSLATED

    # 空筛选：不渲染任何条目，也不残留提示行
    select("keep_declared")
    assert lb.count() == 0
    assert "没有条目" in gui._widgets["review_position"].text()


def test_cursor_clamped_when_filter_shrinks(gui: PytranApp) -> None:
    _goto_review(gui)
    # 游标推到第一页末尾之外
    gui.state._review_index = REVIEW_PAGE_SIZE + 100
    gui._refresh_review()
    assert gui.state._review_index <= gui._displayed_review_rows() - 1
    assert 0 <= gui._widgets["listbox"].currentRow() < gui._displayed_review_rows()


def test_navigation_never_lands_on_hint_row(gui: PytranApp) -> None:
    _goto_review(gui)
    lb = gui._widgets["listbox"]
    lb.setCurrentRow(gui._displayed_review_rows() - 1)
    gui._navigate_list(1)  # 已在末尾，应原地不动而不是跳到提示行
    assert lb.currentRow() == gui._displayed_review_rows() - 1
    assert lb.currentRow() < lb.count() - 1


def test_position_label_reports_rendered_and_total(gui: PytranApp) -> None:
    _goto_review(gui)
    text = gui._widgets["review_position"].text()
    assert str(ALL_ITEMS) in text          # 当前筛选总数
    assert str(REVIEW_PAGE_SIZE) in text   # 已渲染条数
