"""清空工程按钮测试（用户需求 2026-10-20：导入无法清空/反导入）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="清空工程测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from pytran.models import GameFile, Project, Segment, SegmentStatus  # noqa: E402
from pytran.project import JsonProjectStore  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法创建 QApplication：{exc}")
    return app


def _project() -> Project:
    return Project(
        segments=[
            Segment(uid=1, file="a.rpy", locator="# game/a.rpy:1",
                    feature="translate chinese t:", kind="dialogue",
                    comment="", role="", source="hello",
                    status=SegmentStatus.TRANSLATED, target="你好"),
            Segment(uid=2, file="a.rpy", locator="# game/a.rpy:2",
                    feature="translate chinese t:", kind="dialogue",
                    comment="", role="", source="world",
                    status=SegmentStatus.PENDING),
        ],
        files=[GameFile(name="a.rpy", game_relpath="a.rpy")],
        game_root="E:/game",
    )


@pytest.fixture()
def gui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QDialog, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes,
                        raising=False)
    app = PytranApp(_project(), work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def test_clear_button_exists(gui: PytranApp) -> None:
    assert "btn_clear_project" in gui._widgets


def test_clear_project_resets_segments_keeps_root(gui: PytranApp, tmp_path) -> None:
    gui._clear_project()
    assert gui.project.segments == []
    assert gui.project.files == []
    assert gui.project.game_root == "E:/game"      # 游戏目录保留
    assert gui.state.current_step == 1             # 回到导入页
    # project.json 已落盘归零
    reloaded = JsonProjectStore().load(tmp_path)
    assert reloaded.segments == []
    assert reloaded.game_root == "E:/game"


def test_clear_project_confirmation_no(gui: PytranApp, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No,
                        raising=False)
    before = list(gui.project.segments)
    gui._clear_project()
    assert list(gui.project.segments) == before    # 未清空


def test_clear_project_on_empty_is_noop(gui: PytranApp, monkeypatch) -> None:
    gui._clear_project()
    assert gui.project.segments == []
    # 再点一次：空工程提示且不抛
    gui._clear_project()
    assert gui.project.segments == []


def test_clear_then_controls_disabled(gui: PytranApp) -> None:
    gui._clear_project()
    ctrl = gui.state.all_controls()
    assert ctrl["translate"] is False
    assert ctrl["review"] is False
    assert ctrl["export_writeback"] is False
    assert ctrl["import_rpy"] is True
    assert ctrl["tools_unpack"] is True            # game_root 保留
