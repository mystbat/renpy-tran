"""N1-N4 修复的回归测试（问题清单第三轮新增发现）。

* **N1** 成本估算只对待译行求和（旧实现含已译行，实测虚高 2.4 倍）；
* **N2** 「全部导入」全取消勾选 = 不导（旧实现回退全部）；
* **N3** 解包 worker 只发 ``goto_step`` 事件，不直接写 state（§9 红线）；
* **N4** ``engine.stop()`` 后发 ``translate_stopped`` 事件，UI 给明确终态。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="N1-N4 测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QMessageBox, QTreeWidgetItem,
)

from pytran.engine import TranslationEngine  # noqa: E402
from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法创建 QApplication：{exc}")
    return app


@pytest.fixture()
def gui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QDialog, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: None, raising=False)

    # 5 pending + 5 translated：用于验证 N1 的估算口径
    segments = []
    for i in range(1, 6):
        segments.append(Segment(
            uid=i, file="a.rpy", locator=f"l{i}", feature=f"f{i}",
            kind="dialogue", comment="", role="", source="x" * 100,
            status=SegmentStatus.PENDING))
    for i in range(6, 11):
        segments.append(Segment(
            uid=i, file="a.rpy", locator=f"l{i}", feature=f"f{i}",
            kind="dialogue", comment="", role="", source="x" * 100,
            target="译" * 50, status=SegmentStatus.TRANSLATED))
    app = PytranApp(Project(segments=segments, game_root=""), work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


# --------------------------------------------------------------------------- #
# N1：估算只对待译行
# --------------------------------------------------------------------------- #
def test_estimate_excludes_translated_rows(gui: PytranApp) -> None:
    """N1：估算不得计入已译行——旧实现虚高 2.4 倍（实测 21450 vs 9100）。"""
    from pytran.cli import estimate_token_budget
    cfg = gui._current_config()

    actual = gui._estimate_tokens(None)
    only_pending = estimate_token_budget(
        Project(segments=[s for s in gui.project.segments
                          if s.status == SegmentStatus.PENDING]), cfg)
    full = estimate_token_budget(gui.project, cfg)

    assert actual == only_pending, \
        f"估算应只含待译行：got {actual}, want {only_pending}"
    assert actual < full, \
        f"估算必须小于全量口径（全量 {full} 含已译行 target，会虚高）"


def test_translatable_uids_matches_engine_queue(gui: PytranApp) -> None:
    """N1：GUI 的可译口径必须与引擎 _build_queue 严格一致。"""
    ui_uids = set(gui._translatable_uids())
    engine_uids = TranslationEngine._build_queue(gui.project, None)
    assert ui_uids == engine_uids, \
        f"口径漂移：GUI={sorted(ui_uids)} engine={sorted(engine_uids)}"


def test_translatable_uids_excludes_skipped(gui: PytranApp) -> None:
    """SKIPPED 不得进可译集合（设计 §14：宁可不翻不可弄坏）。"""
    gui.project.segments.append(Segment(
        uid=99, file="a.rpy", locator="l", feature="f", kind="skipped",
        comment="", role="", source="def foo(): pass",
        status=SegmentStatus.SKIPPED))
    assert 99 not in gui._translatable_uids()


def test_translate_info_pending_count_matches(gui: PytranApp, monkeypatch) -> None:
    """N1：信息行的「待译 N 行」与可译集合一致。"""
    monkeypatch.setattr(PytranApp, "_estimate_tokens",
                        lambda self, only_uids=None: 1000)
    gui._refresh_translate_info()
    text = gui._widgets["translate_info"].text()
    assert f"待译 {len(gui._translatable_uids())} 行" in text


# --------------------------------------------------------------------------- #
# N2：全取消勾选 = 不导
# --------------------------------------------------------------------------- #
def _fill_import_tree(gui: PytranApp, checked: int, total: int = 3) -> None:
    tree = gui._widgets["import_tree"]
    tree.clear()
    for i in range(total):
        it = QTreeWidgetItem(["", f"file{i}.rpy", "1.0 KB", "未导入"])
        it.setCheckState(0, Qt.CheckState.Checked if i < checked
                         else Qt.CheckState.Unchecked)
        # 与 _refresh_import_tree 一致：路径存于第 1 列（UserRole）
        it.setData(1, Qt.ItemDataRole.UserRole, f"C:/fake/file{i}.rpy")
        tree.addTopLevelItem(it)


def test_all_unchecked_means_import_nothing(gui: PytranApp) -> None:
    """N2：全取消勾选 → 返回空列表（旧实现回退全部，与直觉相反）。"""
    _fill_import_tree(gui, checked=0)
    assert gui._selected_import_files() == [], \
        "全取消勾选应等于「什么都不导」"


def test_partial_unchecked_returns_only_checked(gui: PytranApp) -> None:
    """N2：部分勾选 → 只返回勾中的（9ee271f 第 ⑤ 项的意图）。"""
    _fill_import_tree(gui, checked=2, total=3)
    picked = gui._selected_import_files()
    assert [p.name for p in picked] == ["file0.rpy", "file1.rpy"]


def test_empty_tree_falls_back_to_discovered(gui: PytranApp) -> None:
    """N2：树尚未构建（未刷新）时才回退全部发现。"""
    gui._widgets["import_tree"].clear()
    gui.project.game_root = ""
    gui._pending_rpy_list = []
    gui._pending_rpy = ""
    assert gui._selected_import_files() == []


def test_import_rpy_warns_when_all_unchecked(gui: PytranApp, monkeypatch) -> None:
    """N2：全取消时点「全部导入」应提示，而不是静默导全部。"""
    captured = {}
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *a, **k: captured.setdefault("text", a[2] if len(a) > 2 else ""))
    _fill_import_tree(gui, checked=0)
    started = []
    monkeypatch.setattr(PytranApp, "_do_import_files",
                        lambda self, files: started.append(files))
    # 让「是否有可导入文件」的判据为真：否则走的是「未发现文件」分支
    monkeypatch.setattr(PytranApp, "_get_discovered_rpy_files",
                        lambda self: [Path("C:/fake/file0.rpy")])
    gui._import_rpy()
    assert not started, "全取消勾选时不得启动导入"
    assert "没有勾选" in captured.get("text", ""), \
        "应提示用户「没有勾选任何脚本」"


# --------------------------------------------------------------------------- #
# N3：解包 worker 只发事件
# --------------------------------------------------------------------------- #
def test_goto_step_event_from_unpack_style_worker(gui: PytranApp) -> None:
    """N3：goto_step 事件由主线程消费后切页（§9 红线的一致性验证）。

    直接验证 _apply_event 的 goto_step 分支——这是解包/翻译两条后台路径
    共用的切页通道。
    """
    gui.state.goto(2)
    gui._apply_event("goto_step", {"step": 1})
    assert gui.state.current_step == 1
    assert gui._widgets["stack"].currentIndex() == 1


# --------------------------------------------------------------------------- #
# N4：停止终态
# --------------------------------------------------------------------------- #
def test_translate_stopped_event_sets_final_state(gui: PytranApp) -> None:
    """N4：translate_stopped 事件应复位 busy 态并给出进度快照。"""
    gui._translating = True
    gui._refresh_translate_controls()
    assert gui._widgets["btn_stop"].isEnabled()

    gui._apply_event("translate_stopped",
                     {"processed": 300, "remaining": 42, "round": 2})

    assert gui._translating is False, "停止后应退出忙碌态"
    assert not gui._widgets["btn_stop"].isEnabled()
    assert gui._widgets["btn_translate"].isEnabled()
    text = gui._widgets["progress_label"].text()
    assert "已停止" in text and "300" in text and "42" in text, \
        f"停止终态应含已处理/剩余行数，实际：{text}"


def test_engine_emits_translate_stopped_on_stop(tmp_path) -> None:
    """N4：引擎在 run() 开头检测到 stop 请求时应发 translate_stopped 事件。

    不跑真实翻译：先调 stop() 再 run()，队列非空时应立即 break 并发事件。
    """
    from pytran.config import Config
    from pytran.validators import Validator

    # 用一个永不返回的 provider：若引擎没在开头 break，测试会挂住
    class HangingProvider:
        def translate_batch(self, *a, **k):  # pragma: no cover
            raise AssertionError("不应被调用——stop 后应立即停止")

    eng = TranslationEngine(provider=HangingProvider(), validator=Validator())
    events = []
    rec = lambda n, d: events.append((n, d))

    project = Project(segments=[Segment(
        uid=1, file="a.rpy", locator="l", feature="f", kind="dialogue",
        comment="", role="", source="hello", status=SegmentStatus.PENDING)])
    cfg = Config.load(tmp_path)

    eng.stop()  # 先请求停止
    eng.run(project, cfg, type("P", (), {})(), on_event=rec)

    names = [n for n, _ in events]
    assert "translate_stopped" in names, \
        f"停止后应发 translate_stopped 事件，实际事件：{names}"
    data = next(d for n, d in events if n == "translate_stopped")
    assert data.get("remaining") == 1, "应报告剩余未译行数"
