"""BUGFIX(clear-during-translate)：翻译运行中清空/导入被阻止（防 worker 覆盖竞态）。"""
import os

import pytest

pytest.importorskip("PyQt6", reason="需要 PyQt6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法创建 QApplication：{exc}")


@pytest.fixture()
def gui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QDialog, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes,
                        raising=False)
    app = PytranApp(
        Project(segments=[Segment(uid=1, file="a.rpy", locator="l",
                                  feature="f", kind="dialogue", comment="",
                                  role="", source="x",
                                  status=SegmentStatus.TRANSLATED,
                                  target="旧")]),
        work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    app._translating = True  # 模拟翻译运行中
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def test_clear_blocked_while_translating(gui: PytranApp) -> None:
    """翻译运行中点清空 → 弹警告且不清空（防 worker 覆盖竞态）。"""
    gui._clear_project()
    assert len(gui.project.segments) == 1, "翻译中清空应被拒绝"


def test_import_blocked_while_translating(gui: PytranApp) -> None:
    """翻译运行中导入 → 弹警告且不替换工程。"""
    gui._do_import_files([])
    assert len(gui.project.segments) == 1


def test_clear_allowed_after_stop(gui: PytranApp) -> None:
    """翻译停止后（_translating=False）→ 清空正常执行。"""
    gui._translating = False
    gui._clear_project()
    assert gui.project.segments == []
