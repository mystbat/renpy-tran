"""GUI 层回归：设置对话框保存免翻短语——删除生效、自动豁免保留。"""
import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="需要 PyQt6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget,
)

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
    app = PytranApp(Project(term_profile="profile.json"), work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _open_settings(gui: PytranApp):
    """手动打开设置对话框（对话框由按钮触发，run() 不自动弹）。"""
    gui._open_settings()
    dlg = gui._settings_dialog
    assert dlg is not None
    return dlg


def test_exempt_edit_save_deletes_take_effect(gui: PytranApp, tmp_path) -> None:
    """BUGFIX(exempt-replace)：在免翻输入框里删除条目 → 保存后真正消失。"""
    # 预置 profile：两条豁免（对话框打开前写入，保证初始内容包含它们）
    prof = gui._load_profile()
    prof.exempt_english = ["Nuala", "Aleneite"]
    prof.save(tmp_path / "profile.json")

    dlg = _open_settings(gui)
    # 找到免翻输入框（QPlainTextEdit，初始内容为豁免列表）
    edits = dlg.findChildren(QPlainTextEdit)
    assert edits, "对话框应有文本编辑框"
    exempt_edit = next(e for e in edits if "Nuala" in e.toPlainText())
    # 模拟用户删除 Nuala、新增 MyPhrase
    exempt_edit.setPlainText("Aleneite\nMyPhrase")

    save = next(b for b in dlg.findChildren(QPushButton) if b.text() == "保存")
    save.click()

    after = gui._load_profile()
    assert "Nuala" not in after.exempt_english, "删除的条目应消失"
    assert "Aleneite" in after.exempt_english
    assert "MyPhrase" in after.exempt_english


def test_exempt_auto_from_empty_dst_preserved(gui: PytranApp, tmp_path) -> None:
    """术语表空译名行自动推导的豁免，在免翻框被清空时仍保留（不丢）。"""
    # 预置：豁免列表非空（对话框初始内容含它）
    prof = gui._load_profile()
    prof.exempt_english = ["Nuala"]
    prof.save(tmp_path / "profile.json")

    dlg = _open_settings(gui)
    # 术语表：Mandy 无译名 → auto_exempt=['Mandy']
    from PyQt6.QtWidgets import QTableWidgetItem
    table = next(t for t in dlg.findChildren(QTableWidget))
    table.setRowCount(1)
    table.setItem(0, 0, QTableWidgetItem("Mandy"))
    table.setItem(0, 1, QTableWidgetItem(""))

    edits = dlg.findChildren(QPlainTextEdit)
    exempt_edit = next(e for e in edits if "Nuala" in e.toPlainText())
    exempt_edit.setPlainText("")  # 用户清空免翻框

    save = next(b for b in dlg.findChildren(QPushButton) if b.text() == "保存")
    save.click()

    after = gui._load_profile()
    assert "Mandy" in after.exempt_english, "空译名自动豁免应保留"
    assert "Nuala" not in after.exempt_english