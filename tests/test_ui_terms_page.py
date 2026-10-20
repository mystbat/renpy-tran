"""术语表页 P2 修复的回归测试。

覆盖问题清单（``docs/翻译选项卡问题清单.md``）：

* **B3**：「从工程抽取候选」旧实现把候选直接 append 进表格，第二列写死
  ``(出现 N 次)`` 伪译名——用户直接保存会把该字符串写进 ``terms``；
  且 W5b §5 要求「工程为空时抽取置灰」。
* **B4**：术语表页缺「命中术语的英文不判为含英文（白名单）」复选框，
  ``terms.apply_terms(exempt_enabled=)`` 从未被 UI 使用。
* **B6**：缺「重复原文合并提示 / 保存结果汇总」，
  ``terms.detect_duplicate_originals`` 从未被 UI 使用。
* **B8**：``profile.json`` 损坏时按空 profile 处理并告警（W5b §5 边界）。
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("PyQt6", reason="术语表页测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QMessageBox, QPushButton, QTableWidget, QCheckBox,
    QTableWidgetItem, QPlainTextEdit,
)

from pytran.config import TermProfile  # noqa: E402
from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.terms import Candidate  # noqa: E402
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

    segments = [
        Segment(uid=i, file="a.rpy", locator=f"l{i}", feature=f"f{i}",
                kind="dialogue", comment="", role="",
                source="Ian and Lena went to the Stalkfap again" if i == 1
                       else f"line {i}",
                status=SegmentStatus.PENDING)
        for i in range(1, 6)
    ]
    app = PytranApp(Project(segments=segments, game_root=""), work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _open_settings(gui: PytranApp) -> QDialog:
    gui._open_settings()
    return gui._settings_dialog


def _terms_table(dlg: QDialog) -> QTableWidget:
    return dlg.findChildren(QTableWidget)[0]


def _save(dlg: QDialog) -> None:
    [b for b in dlg.findChildren(QPushButton) if b.text() == "保存"][0].click()


def _checkbox(dlg: QDialog) -> QCheckBox:
    boxes = dlg.findChildren(QCheckBox)
    assert boxes, "术语表页应有白名单复选框（B4）"
    return boxes[0]


# --------------------------------------------------------------------------- #
# B3：候选对话框
# --------------------------------------------------------------------------- #
def test_pick_term_candidates_returns_only_filled_or_english(gui: PytranApp) -> None:
    """B3：候选对话框只并入「填了译名」或「原文含英文」的条目。

    纯中文候选且译名留空 → 视为忽略，不得写进 terms。
    """
    parent = _open_settings(gui)
    # 用中文候选验证「留空即忽略」：Lena 是外文（会被当保留原文并入），
    # 所以这里用一个纯中文候选来验证忽略路径。
    cands = [Candidate("Ian", 5, "D"), Candidate("李明", 3, "D"),
             Candidate("Stalkfap", 2, "B")]

    from PyQt6.QtWidgets import QDialog as _QDialog
    orig_exec = _QDialog.exec

    def fake_exec(self):
        table = self.findChildren(QTableWidget)[0]
        assert table.rowCount() == 3
        # 行序与 cands 一致：0=Ian, 1=李明, 2=Stalkfap
        # 列序（W5c）：0=候选词 1=来源 2=出现次数 3=译名
        assert table.item(0, 1).text() == "D", "来源列应展示信号源标记"
        table.setItem(0, 3, QTableWidgetItem("伊恩"))   # 填译名 → 并入
        # 1 留空且纯中文 → 应被忽略
        # 2 留空但含英文 → 按原文保留并入
        return 1  # 模拟 Accepted

    _QDialog.exec = fake_exec
    try:
        out = gui._pick_term_candidates(parent, cands)
    finally:
        _QDialog.exec = orig_exec

    assert ("Ian", "伊恩") in out
    assert ("Stalkfap", "") in out, "含英文且译名留空 → 按原文保留，应并入"
    assert not any(t == "李明" for t, _ in out), \
        "纯中文候选且译名留空 → 应被忽略，不得写入无译名的 terms"


def test_pick_term_candidates_cancel_returns_empty(gui: PytranApp) -> None:
    """B3：用户点「取消」不得并入任何候选。"""
    parent = _open_settings(gui)
    from PyQt6.QtWidgets import QDialog as _QDialog
    orig_exec = _QDialog.exec
    _QDialog.exec = lambda self: 0  # Rejected
    try:
        out = gui._pick_term_candidates(
            parent, [Candidate("Ian", 5, "D"), Candidate("Stalkfap", 2, "B")])
    finally:
        _QDialog.exec = orig_exec
    assert out == [], "取消后不得并入任何候选"


def test_extract_button_disabled_when_project_empty(gui: PytranApp, monkeypatch) -> None:
    """B3 / W5b §5：工程为空时「从工程抽取候选」必须置灰。"""
    gui.project.segments.clear()
    monkeypatch.setattr(PytranApp, "_current_config", lambda self: type("C", (), {})())
    dlg = _open_settings(gui)
    btn = [b for b in dlg.findChildren(QPushButton)
           if b.text() == "从工程抽取候选…"][0]
    assert not btn.isEnabled(), "工程为空时抽取按钮应置灰"

    # 有段落时应可用
    gui.project.segments.append(
        Segment(uid=1, file="a.rpy", locator="l", feature="f", kind="dialogue",
                comment="", role="", source="Ian", status=SegmentStatus.PENDING))
    dlg2 = _open_settings(gui)
    btn2 = [b for b in dlg2.findChildren(QPushButton)
            if b.text() == "从工程抽取候选…"][0]
    assert btn2.isEnabled()


# --------------------------------------------------------------------------- #
# B4：白名单复选框
# --------------------------------------------------------------------------- #
def _exempt_edit(dlg: QDialog):
    """术语表页的「免翻英文短语」输入框。

    对话框里有两个 QPlainTextEdit（术语表的豁免框 + 翻译规则页的规则框）。
    两者初始都可能是空文本，无法按内容区分——改用几何特征：
    豁免框设置了 setMaximumHeight(80)，规则框没有。
    """
    from PyQt6.QtWidgets import QPlainTextEdit
    for e in dlg.findChildren(QPlainTextEdit):
        if e.maximumHeight() == 80:
            return e
    raise AssertionError("未找到免翻英文短语输入框（应有 maximumHeight=80）")


def test_whitelist_checkbox_present_and_toggles_exempt(gui: PytranApp) -> None:
    """B4：白名单复选框存在，且控制「免翻英文短语」输入框是否生效。"""
    dlg = _open_settings(gui)
    box = _checkbox(dlg)
    assert "白名单" in box.text()

    # 取消勾选 → 输入框里的短语不应进入 exempt_english
    exempt = _exempt_edit(dlg)
    exempt.setPlainText("ManualPhrase")
    box.setChecked(False)
    _save(dlg)

    profile = TermProfile.load(gui.work_dir / "profile.json")
    assert "ManualPhrase" not in profile.exempt_english, \
        "取消勾选白名单时，输入框短语不应生效（B4）"

    # 勾选 → 生效
    dlg2 = _open_settings(gui)
    box2 = _checkbox(dlg2)
    _exempt_edit(dlg2).setPlainText("ManualPhrase")
    box2.setChecked(True)
    _save(dlg2)
    profile2 = TermProfile.load(gui.work_dir / "profile.json")
    assert "ManualPhrase" in profile2.exempt_english


# --------------------------------------------------------------------------- #
# B6：重复原文合并提示 + 保存汇总
# --------------------------------------------------------------------------- #
def test_save_summary_reports_terms_and_duplicates(gui: PytranApp) -> None:
    """B6：保存弹窗应汇总术语/豁免数量，并提示重复原文已合并。"""
    captured = {}
    orig = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: captured.setdefault("text", a[2] if len(a) > 2 else k.get("text")))
    try:
        dlg = _open_settings(gui)
        table = _terms_table(dlg)
        # 两行同一原文（后值覆盖）
        for name, trans in (("Ian", "伊恩"), ("Ian", "伊恩二世")):
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(name))
            table.setItem(r, 1, QTableWidgetItem(trans))
        _save(dlg)
    finally:
        QMessageBox.information = orig

    text = captured.get("text", "")
    assert "术语" in text and "条" in text, "弹窗应汇总术语条数"
    assert "重复原文已合并" in text, "弹窗应提示重复原文已合并（B6）"
    assert "Ian" in text

    # 后值覆盖生效
    profile = TermProfile.load(gui.work_dir / "profile.json")
    assert profile.terms.get("Ian") == "伊恩二世"


# --------------------------------------------------------------------------- #
# B8：profile.json 损坏
# --------------------------------------------------------------------------- #
def test_corrupt_profile_falls_back_to_empty_with_warning(
    gui: PytranApp, monkeypatch, tmp_path
) -> None:
    """B8：profile.json 损坏 → 空 profile + 告警，不得抛异常。"""
    (tmp_path / "profile.json").write_text("{ this is not valid json", encoding="utf-8")
    warnings = []
    monkeypatch.setattr(gui, "_log",
                        lambda msg, level="info": warnings.append((msg, level)))

    profile = gui._load_profile()
    assert isinstance(profile, TermProfile)
    assert profile.terms == {}, "损坏 profile 应按空术语表处理"
    assert warnings and warnings[0][1] == "warn", "应发出 warn 级告警"
