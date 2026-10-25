"""W5h 全面审校 的无头回归测试。

锁定行为（任务书 §2 / §5）：
* ``UiState.full_audit`` 纯函数，对全量段做 V1–V6 + 术语未生效 检查，
  返回分类问题列表，**不改任何段落数据**；
* 标签/转义/引号/英文/术语 五类都能被抓出（各自命中对应文本）；
* PENDING 且无 target 的段被跳过；
* 空工程不崩溃；
* 双击定位：把审校页切到 uid 对应段（只读，不改数据）。

PyQt6 属于可选 GUI 依赖：未安装时整体跳过。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="全面审校测试需要 PyQt6（GUI 依赖，可选）")

# 无显示环境下用 offscreen 平台，必须在 QApplication 之前设置。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from pytran.config import TermProfile  # noqa: E402
from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402
from pytran.ui_state import UiState  # noqa: E402


def _seg(uid: int, source: str, target: str,
         status: SegmentStatus = SegmentStatus.TRANSLATED) -> Segment:
    return Segment(
        uid=uid, file="a.rpy", locator=f"game/a.rpy:{uid}",
        feature=f"translate chinese seg_{uid}:", kind="dialogue",
        comment="", role="", source=source, target=target, status=status,
    )


def _make_project(segments: list[Segment]) -> Project:
    return Project(game_root="C:/game", segments=segments)


@pytest.fixture(scope="module")
def qt_app():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover - 平台不支持时跳过
        pytest.skip(f"无法创建 QApplication：{exc}")
    return app


@pytest.fixture()
def gui(qt_app, monkeypatch, request, tmp_path):
    """构建真实窗口（不进入事件循环），返回 PytranApp。"""
    segments = list(getattr(request, "param", []))
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    # 隔离工作目录：绝不能用 "."——真实应用的 work_dir 就是仓库根，
    # 那里放着用户自己的 project.json / profile.json（见 conftest 守卫）。
    app = PytranApp(_make_project(segments), work_dir=str(tmp_path))
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _cat_texts(results: list[dict]) -> list[str]:
    out: list[str] = []
    for r in results:
        for cat, detail in r["checks"]:
            out.append(f"{cat}|{detail}")
    return out


# ---------------------------------------------------------------------------
# 1. 全干净 → 0 问题
# ---------------------------------------------------------------------------
def test_all_clean_produces_no_issues() -> None:
    proj = _make_project([
        _seg(1, "Hello world", "你好世界"),
        _seg(2, "Goodbye", "再见"),
    ])
    res = UiState(proj).full_audit(TermProfile())
    assert res == []


# ---------------------------------------------------------------------------
# 2. 标签丢失：{size=40} 消失 → 命中“标签”
# ---------------------------------------------------------------------------
def test_tag_lost_flagged() -> None:
    proj = _make_project([
        _seg(1, "He {size=40}smiled{/size}", "他笑了"),  # 丢了 {size=40}
    ])
    res = UiState(proj).full_audit(TermProfile())
    texts = _cat_texts(res)
    assert any("标签" in t for t in texts)
    assert res[0]["uid"] == 1


# ---------------------------------------------------------------------------
# 3. 转义丢失：\n 消失 → 命中“转义”
# ---------------------------------------------------------------------------
def test_escape_lost_flagged() -> None:
    proj = _make_project([
        _seg(1, "Line one\\nLine two", "第一行第二行"),  # 丢了 \n 转义
    ])
    res = UiState(proj).full_audit(TermProfile())
    texts = _cat_texts(res)
    assert any("转义" in t for t in texts)


# ---------------------------------------------------------------------------
# 4. 英文双引号未修复 → 命中“引号”
# ---------------------------------------------------------------------------
def test_english_quote_unrepaired_flagged() -> None:
    proj = _make_project([
        _seg(1, 'He said "hi"', '"他说了 hi"'),  # 译文仍含 ASCII 双引号
    ])
    res = UiState(proj).full_audit(TermProfile())
    texts = _cat_texts(res)
    assert any("引号" in t for t in texts)


# ---------------------------------------------------------------------------
# 5. 英文残余 → 命中“英文”
# ---------------------------------------------------------------------------
def test_english_residual_flagged() -> None:
    proj = _make_project([
        _seg(1, "Open the door", "Open 门"),  # 残留 Open
    ])
    res = UiState(proj).full_audit(TermProfile())
    texts = _cat_texts(res)
    assert any("英文" in t for t in texts)


# ---------------------------------------------------------------------------
# 6. 空译文（status 非 PENDING 但 target 空）→ 命中“译文为空”
# ---------------------------------------------------------------------------
def test_empty_target_flagged() -> None:
    proj = _make_project([
        _seg(1, "Hello", "", status=SegmentStatus.REVIEWED),
    ])
    res = UiState(proj).full_audit(TermProfile())
    texts = _cat_texts(res)
    assert any("译文为空" in t for t in texts)


# ---------------------------------------------------------------------------
# 7. 术语未生效：原文含术语 key，译文不含译名 → 命中“术语未生效”
# ---------------------------------------------------------------------------
def test_term_not_applied_flagged() -> None:
    profile = TermProfile(terms={"Ian": "伊恩"})
    proj = _make_project([
        _seg(1, "Ian went home", "他回家了"),  # 漏译 Ian → 伊恩
    ])
    res = UiState(proj).full_audit(profile)
    texts = _cat_texts(res)
    assert any("术语未生效" in t for t in texts)


# ---------------------------------------------------------------------------
# 8. 术语已生效：原文含术语 key 且译文含译名 → 不报术语
# ---------------------------------------------------------------------------
def test_term_applied_not_flagged() -> None:
    profile = TermProfile(terms={"Ian": "伊恩"})
    proj = _make_project([
        _seg(1, "Ian went home", "伊恩回家了"),
    ])
    res = UiState(proj).full_audit(profile)
    texts = _cat_texts(res)
    assert not any("术语未生效" in t for t in texts)


# ---------------------------------------------------------------------------
# 9. 双击定位：切到该 uid 所在审校行（只读）
# ---------------------------------------------------------------------------
def test_double_click_locates_uid(qt_app, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    proj = _make_project([
        _seg(1, "Hello", "你好"),
        _seg(2, "He {size=40}smiled{/size}", "他笑了"),  # 问题段 uid=2
        _seg(3, "Goodbye", "再见"),
    ])
    app = PytranApp(proj, work_dir=str(tmp_path))
    app._log = lambda *a, **k: None
    app.run()
    try:
        app._audit_results = app.state.full_audit(TermProfile())
        app._locate_audit_uid(2)
        # 切到审校页、游标指向 uid=2 对应行（all 筛选下为第 2 行）
        assert app._widgets["stack"].currentIndex() == 3
        assert app.state.current_review() is not None
        assert app.state.current_review().uid == 2
    finally:
        if app._timer is not None:
            app._timer.stop()
        if app._window is not None:
            app._window.close()


# ---------------------------------------------------------------------------
# 10. 空工程：不崩溃，返回 []
# ---------------------------------------------------------------------------
def test_empty_project_no_crash() -> None:
    proj = _make_project([])
    res = UiState(proj).full_audit(TermProfile())
    assert res == []


# ---------------------------------------------------------------------------
# 11. PENDING 且 target 为空 → 跳过不报
# ---------------------------------------------------------------------------
def test_pending_empty_target_skipped() -> None:
    proj = _make_project([
        _seg(1, "Hello world", "", status=SegmentStatus.PENDING),
    ])
    res = UiState(proj).full_audit(TermProfile())
    assert res == []


# ---------------------------------------------------------------------------
# 12. 不改任何段落数据（只读检查）
# ---------------------------------------------------------------------------
def test_full_audit_is_readonly() -> None:
    segs = [
        _seg(1, "He {size=40}smiled{/size}", "他笑了"),
        _seg(2, "Ian left", "他离开了", status=SegmentStatus.PENDING),
    ]
    proj = _make_project(segs)
    UiState(proj).full_audit(TermProfile(terms={"Ian": "伊恩"}))
    assert [s.status for s in proj.segments] == [SegmentStatus.TRANSLATED,
                                                 SegmentStatus.PENDING]
    assert [s.target for s in proj.segments] == ["他笑了", "他离开了"]


# ---------------------------------------------------------------------------
# 13. GUI 面板：有问题时按钮产出非空结果且能构建面板
# ---------------------------------------------------------------------------
def test_gui_full_audit_button_populates_results(qt_app, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    proj = _make_project([
        _seg(1, "He {size=40}smiled{/size}", "他笑了"),
        _seg(2, "Ian left", "他离开了"),  # 同时漏译术语 Ian
    ])
    profile = TermProfile(terms={"Ian": "伊恩"})
    app = PytranApp(proj, work_dir=str(tmp_path))
    app._log = lambda *a, **k: None
    app.run()
    try:
        # 直接构建面板（不触发 exec，避免无头环境阻塞）
        app._audit_results = app.state.full_audit(profile)
        dlg = app._build_audit_dialog(app._audit_results)
        assert dlg is not None
        # QTableWidget 按 (段, 分类) 逐条展开：uid1 的 {size=40} 与 {/size}
        # 是两条独立标签问题，uid2 是术语未生效 → 共 3 行
        from PyQt6.QtWidgets import QTableWidget
        table = dlg.findChild(QTableWidget)
        assert table is not None and table.rowCount() == 3
        # 行首 cell 用 UserRole 携带 uid，供双击定位
        assert table.item(0, 0).data(Qt.ItemDataRole.UserRole) == 1
        assert table.item(1, 0).data(Qt.ItemDataRole.UserRole) == 1
        assert table.item(2, 0).data(Qt.ItemDataRole.UserRole) == 2
    finally:
        if app._timer is not None:
            app._timer.stop()
        if app._window is not None:
            app._window.close()


# ---------------------------------------------------------------------------
# 14. 重译按钮：命中段落全部置 FAILED 并落盘，其余不变
# ---------------------------------------------------------------------------
def test_retranslate_marks_failed(tmp_path, qt_app, monkeypatch) -> None:
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    proj = _make_project([
        _seg(1, "He {size=40}smiled{/size}", "他笑了"),     # 命中：标签
        _seg(2, "Ian left", "他离开了"),                     # 命中：术语
        _seg(3, "All good here", "这里一切正常"),            # 未命中，保持
    ])
    profile = TermProfile(terms={"Ian": "伊恩"})
    app = PytranApp(proj, work_dir=str(tmp_path))
    app._log = lambda *a, **k: None
    app.run()
    try:
        app._audit_results = app.state.full_audit(profile)
        assert {r["uid"] for r in app._audit_results} == {1, 2}
        dlg = app._build_audit_dialog(app._audit_results)
        app._on_audit_retranslate(dlg)  # 点击「重译」
        # uid1、uid2 置 FAILED；uid3 不变
        by_uid = {s.uid: s for s in app.project.segments}
        assert by_uid[1].status == SegmentStatus.FAILED
        assert by_uid[2].status == SegmentStatus.FAILED
        assert by_uid[3].status == SegmentStatus.TRANSLATED
        # 已落盘（project.json 写入临时目录）
        assert (tmp_path / "project.json").exists()
        # 面板已闭合、结果清空
        assert app._audit_dialog is None
        assert app._audit_results == []
    finally:
        if app._timer is not None:
            app._timer.stop()
        if app._window is not None:
            app._window.close()


# ---------------------------------------------------------------------------
# 15. 重译幂等：已是 FAILED 的命中段不覆盖其 error 原因
# ---------------------------------------------------------------------------
def test_retranslate_idempotent_on_failed(tmp_path, qt_app, monkeypatch) -> None:
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    flagged = _seg(1, "He {size=40}smiled{/size}", "他笑了")
    flagged.status = SegmentStatus.FAILED
    flagged.error = "上次翻译失败：超时"
    proj = _make_project([flagged])
    app = PytranApp(proj, work_dir=str(tmp_path))
    app._log = lambda *a, **k: None
    app.run()
    try:
        app._audit_results = app.state.full_audit(TermProfile())
        assert app._audit_results and app._audit_results[0]["uid"] == 1
        dlg = app._build_audit_dialog(app._audit_results)
        app._on_audit_retranslate(dlg)
        seg = app.project.segments[0]
        # 仍为 FAILED，但原有失败原因（非“全面审校发现问题”）被保留
        assert seg.status == SegmentStatus.FAILED
        assert seg.error == "上次翻译失败：超时"
    finally:
        if app._timer is not None:
            app._timer.stop()
        if app._window is not None:
            app._window.close()
