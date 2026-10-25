"""「AI 校对」按钮的无头回归测试（用户 2026-10-24 决策）。

锁定行为：

* 全面审校面板底部有独立的「AI 校对」按钮（不自动跑——token 由用户主动花）；
* 后台复核结论以 ``[AI 校对]`` 分类**并入同一张表**，双击可定位；
* 「重译」按钮覆盖规则问题与 AI 问题（同一张表、同一处理路径）；
* 后台线程不直接改控件——只发 ``audit_review_progress`` / ``audit_review_done``
  事件，由主线程 ``_apply_event`` 落到表格。

PyQt6 属于可选 GUI 依赖：未安装时整体跳过。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="AI 校对测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPushButton, QTableWidget  # noqa: E402

from pytran.config import TermProfile  # noqa: E402
from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


def _seg(uid: int, source: str, target: str,
         status: SegmentStatus = SegmentStatus.TRANSLATED) -> Segment:
    return Segment(
        uid=uid, file="a.rpy", locator=f"game/a.rpy:{uid}",
        feature=f"translate chinese seg_{uid}:", kind="dialogue",
        comment="", role="", source=source, target=target, status=status,
    )


@pytest.fixture(scope="module")
def qt_app():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover - 平台不支持时跳过
        pytest.skip(f"无法创建 QApplication：{exc}")
    return app


@pytest.fixture()
def gui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    proj = Project(game_root="C:/game", segments=[
        _seg(1, "Hello", "你好"),
        _seg(2, "Goodbye", "再见"),
    ])
    # 隔离工作目录：绝不能用 "."——真实应用的 work_dir 就是仓库根，
    # 那里放着用户自己的 project.json / profile.json（见 conftest 守卫）。
    app = PytranApp(proj, work_dir=str(tmp_path))
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _buttons(dlg) -> dict[str, QPushButton]:
    return {b.text(): b for b in dlg.findChildren(QPushButton)}


class _FakeProvider:
    """复核通道替身：把「哪些 uid 判 NG」写死在响应里。"""

    def __init__(self, ng: dict[int, str] | None = None,
                 omit: tuple[int, ...] = ()) -> None:
        self.ng = ng or {}
        self.omit = omit
        self.calls: list[int] = []

    def review_text(self, prompt, system="", *, max_tokens=None, line_count=0):
        uids = [int(ln.split("\t")[0])
                for ln in prompt.splitlines()
                if "\t" in ln and ln.split("\t")[0].strip().isdigit()]
        self.calls.append(len(uids))
        out = []
        for uid in uids:
            if uid in self.omit:
                continue
            out.append(f"{uid}|NG|{self.ng[uid]}" if uid in self.ng else f"{uid}|ok")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# 1. 面板里有独立的「AI 校对」按钮
# ---------------------------------------------------------------------------
def test_dialog_has_ai_review_button(gui) -> None:
    gui._audit_results = gui.state.full_audit(TermProfile())
    dlg = gui._build_audit_dialog(gui._audit_results)
    btns = _buttons(dlg)
    assert "AI 校对" in btns
    assert "重译" in btns
    assert btns["AI 校对"].isEnabled()


# ---------------------------------------------------------------------------
# 2. AI 结论并入同一张表（分类 = AI 校对）
# ---------------------------------------------------------------------------
def test_ai_issues_merged_into_same_table(gui) -> None:
    gui._audit_results = []          # 规则检查全干净
    dlg = gui._build_audit_dialog(gui._audit_results)
    table = dlg.findChild(QTableWidget)
    assert table.rowCount() == 0

    gui._apply_ai_review_result({
        "issues": [(2, "译文是别的行的内容")],
        "unconfirmed": [], "errors": [], "windows": 1, "rows": 2,
    })
    assert table.rowCount() == 1
    assert table.item(0, 0).data(Qt.ItemDataRole.UserRole) == 2
    assert "[AI 校对]" in table.item(0, 2).text()
    assert "别的行" in table.item(0, 2).text()


# ---------------------------------------------------------------------------
# 3. 未确认行也进表（「模型什么都不报」必须可见）
# ---------------------------------------------------------------------------
def test_unconfirmed_rows_are_visible(gui) -> None:
    gui._audit_results = []
    dlg = gui._build_audit_dialog(gui._audit_results)
    table = dlg.findChild(QTableWidget)

    gui._apply_ai_review_result({
        "issues": [], "unconfirmed": [1],
        "errors": [], "windows": 1, "rows": 2,
    })
    assert table.rowCount() == 1
    assert "未确认" in table.item(0, 2).text()


# ---------------------------------------------------------------------------
# 4. 同一段既有规则问题又有 AI 问题 → 两行、不重复建段条目
# ---------------------------------------------------------------------------
def test_ai_issue_appends_to_existing_segment_entry(gui) -> None:
    gui.project.segments[0].target = "他笑了"      # 丢标签 → 规则命中
    gui.project.segments[0].source = "He {size=40}smiled{/size}"
    gui._audit_results = gui.state.full_audit(TermProfile())
    n_rule = len(gui._audit_results)
    dlg = gui._build_audit_dialog(gui._audit_results)
    table = dlg.findChild(QTableWidget)
    rows_before = table.rowCount()

    gui._apply_ai_review_result({
        "issues": [(1, "漏译")], "unconfirmed": [],
        "errors": [], "windows": 1, "rows": 1,
    })
    assert len(gui._audit_results) == n_rule      # 段条目没被重复添加
    assert table.rowCount() == rows_before + 1


# ---------------------------------------------------------------------------
# 5. 重译按钮同时覆盖 AI 问题
# ---------------------------------------------------------------------------
def test_retranslate_covers_ai_issues(gui) -> None:
    gui._audit_results = []
    dlg = gui._build_audit_dialog(gui._audit_results)
    gui._apply_ai_review_result({
        "issues": [(2, "译文与原文无关")], "unconfirmed": [],
        "errors": [], "windows": 1, "rows": 2,
    })
    gui._on_audit_retranslate(dlg)
    by_uid = {s.uid: s for s in gui.project.segments}
    assert by_uid[2].status == SegmentStatus.FAILED
    assert by_uid[1].status == SegmentStatus.TRANSLATED


# ---------------------------------------------------------------------------
# 6. 后台线程只发事件：进度/终态都走 _apply_event
# ---------------------------------------------------------------------------
def test_ai_review_runs_off_main_thread(gui) -> None:
    fake = _FakeProvider(ng={2: "译文与原文无关"})
    gui.deps = type("D", (), {"provider": fake, "config": gui._current_config(),
                              "engine": None})()
    gui._audit_results = []
    dlg = gui._build_audit_dialog(gui._audit_results)
    table = dlg.findChild(QTableWidget)

    gui._on_ai_review()
    assert gui._audit_reviewing is True
    assert gui._audit_review_btn.isEnabled() is False

    # 等后台线程把终态事件塞进队列，再由主线程排空
    for _ in range(200):
        gui._drain_events()
        if not gui._audit_reviewing:
            break
        QApplication.processEvents()
        import time
        time.sleep(0.02)

    assert gui._audit_reviewing is False
    assert gui._audit_review_btn.isEnabled() is True
    assert fake.calls == [2]              # 一次请求覆盖两行
    assert table.rowCount() == 1
    assert "[AI 校对]" in table.item(0, 2).text()


# ---------------------------------------------------------------------------
# 7. 没有 provider（未配 Key）→ 提示而不崩
# ---------------------------------------------------------------------------
def test_ai_review_without_provider_logs(gui, monkeypatch) -> None:
    logs: list[str] = []
    gui._log = lambda msg, **k: logs.append(str(msg))
    gui.deps = type("D", (), {"provider": None, "config": gui._current_config(),
                              "engine": None})()
    monkeypatch.setattr(gui, "_build_default_deps",
                        lambda **k: type("D", (), {
                            "provider": None,
                            "config": gui._current_config(),
                            "engine": None})())
    gui._audit_results = []
    gui._build_audit_dialog(gui._audit_results)
    gui._on_ai_review()
    assert gui._audit_reviewing is False
    assert any("API Key" in m for m in logs)


# ---------------------------------------------------------------------------
# 8. 重复点击被拦住（同一时间只跑一次）
# ---------------------------------------------------------------------------
def test_ai_review_not_reentrant(gui) -> None:
    gui._audit_reviewing = True
    logs: list[str] = []
    gui._log = lambda msg, **k: logs.append(str(msg))
    gui._on_ai_review()
    assert any("正在进行" in m for m in logs)


# ---------------------------------------------------------------------------
# 9. 面板关闭 → 停止标志置位，结果清空
# ---------------------------------------------------------------------------
def test_closing_dialog_requests_stop(gui) -> None:
    gui._audit_results = []
    gui._build_audit_dialog(gui._audit_results)
    gui._clear_audit()
    assert gui._audit_review_stop is True
    assert gui._audit_reviewing is False
    assert gui._audit_results == []
    assert gui._audit_table is None


# ---------------------------------------------------------------------------
# 10. review_enabled=关 → 按钮禁用 + 点击被拒（不偷偷花 token）
# ---------------------------------------------------------------------------
def test_review_disabled_disables_button_and_blocks_run(gui, monkeypatch) -> None:
    monkeypatch.setattr(gui, "_current_config",
                        lambda: type("C", (), {"review_enabled": False})())
    gui._audit_results = []
    dlg = gui._build_audit_dialog(gui._audit_results)
    assert _buttons(dlg)["AI 校对"].isEnabled() is False

    logs: list[str] = []
    gui._log = lambda msg, **k: logs.append(str(msg))
    gui._on_ai_review()
    assert gui._audit_reviewing is False
    assert any("复核开关" in m for m in logs)


# ---------------------------------------------------------------------------
# 11. 连点两次「AI 校对」不重复堆同样的结论
# ---------------------------------------------------------------------------
def test_repeat_review_does_not_duplicate_rows(gui) -> None:
    gui._audit_results = []
    dlg = gui._build_audit_dialog(gui._audit_results)
    table = dlg.findChild(QTableWidget)
    payload = {"issues": [(2, "译文与原文无关")], "unconfirmed": [1],
               "errors": [], "windows": 1, "rows": 2}
    gui._apply_ai_review_result(payload)
    assert table.rowCount() == 2
    gui._apply_ai_review_result(payload)      # 第二轮：同样的结论
    assert table.rowCount() == 2              # 不重复追加
    assert len(gui._audit_results) == 2
