"""翻译进度条状态测试（BUGFIX(busy-stuck) + 本次运行细条）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="进度条测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from pytran.models import Project  # noqa: E402
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
    app = PytranApp(Project(), work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def test_stop_resets_busy_mode(gui: PytranApp) -> None:
    """BUGFIX(busy-stuck)：round 切 indeterminate 后收到 translate_stopped，
    进度条必须恢复确定性模式（不再流动）。"""
    gui._apply_event("round", {"round": 1, "queue_size": 10})
    pb = gui._widgets["progress"]
    assert pb.maximum() == 0  # 翻译中：indeterminate

    gui._apply_event("translate_stopped", {"processed": 5, "remaining": 5})
    assert pb.maximum() == 100  # 已恢复确定性
    assert not gui._translating
    assert gui._widgets["run_progress"].isHidden()


def test_finish_resets_busy_mode(gui: PytranApp) -> None:
    """translate_finished 终态同样恢复确定性模式。"""
    gui._apply_event("round", {"round": 1, "queue_size": 10})
    gui._apply_event("translate_finished", {})
    assert gui._widgets["progress"].maximum() == 100
    assert not gui._translating


def test_run_progress_lifecycle(gui: PytranApp) -> None:
    """细条生命周期：round 显示并清零 → chunk_done 推进 → 终态隐藏。"""
    rp = gui._widgets["run_progress"]
    assert rp.isHidden()

    gui._apply_event("round", {"round": 1, "queue_size": 40})
    assert not rp.isHidden()
    assert rp.value() == 0

    gui._apply_event("chunk_done", {"done": 1, "total": 4, "rows": 10})
    assert rp.value() == 25  # 10/40

    gui._apply_event("chunk_done", {"done": 2, "total": 4, "rows": 10})
    assert rp.value() == 50

    gui._apply_event("translate_finished", {})
    assert rp.isHidden()


def test_run_progress_second_run_resets(gui: PytranApp) -> None:
    """再次开跑：细条重新清零显示（不复用上次的残留值）。"""
    rp = gui._widgets["run_progress"]
    gui._apply_event("round", {"round": 1, "queue_size": 10})
    gui._apply_event("chunk_done", {"done": 1, "total": 1, "rows": 10})
    gui._apply_event("translate_finished", {})

    gui._apply_event("round", {"round": 1, "queue_size": 20})
    assert not rp.isHidden()
    assert rp.value() == 0


def test_chunk_done_updates_main_progress(gui: PytranApp) -> None:
    """主条仍按块推进（既有行为回归守卫）。"""
    pb = gui._widgets["progress"]
    gui._apply_event("chunk_done", {"done": 3, "total": 4, "rows": 5})
    assert pb.value() == 75


def test_drain_events_final_guard(gui: PytranApp) -> None:
    """BUGFIX(busy-stuck) 最终防线：round 事件在 finished 之后才被消费
    （事件乱序/交叠）时，_drain_events 排空后强制恢复确定性——
    用户实测：工作完成后绿色流动永不停止。"""
    pb = gui._widgets["progress"]
    # 正常完成
    gui._apply_event("translate_finished", {})
    assert pb.maximum() == 100
    # 迟到的 round 事件（乱序）把进度条切回 indeterminate
    gui._apply_event("round", {"round": 2, "queue_size": 5})
    assert pb.maximum() == 0  # 流动中
    # 事件排空（_drain_events 的 finally 兜底）→ 恢复
    gui._drain_events()
    assert pb.maximum() == 100, "排空后必须恢复确定性模式"


def test_drain_guard_does_not_fight_running_translation(gui: PytranApp) -> None:
    """防线不误伤：翻译进行中（_translating=True）时排空不恢复模式。"""
    pb = gui._widgets["progress"]
    gui._translating = True
    gui._apply_event("round", {"round": 1, "queue_size": 10})
    assert pb.maximum() == 0
    gui._drain_events()
    assert pb.maximum() == 0  # 仍在翻译，保持 indeterminate
    gui._translating = False
    gui._drain_events()
    assert pb.maximum() == 100  # 停止后可恢复
