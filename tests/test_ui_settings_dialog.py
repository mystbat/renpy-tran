"""设置对话框的 GUI 级回归测试（P0 止血配套，问题清单 A1/A2/A3/B5）。

背景（``docs/翻译选项卡问题清单.md``）：
设置对话框的 ``save()`` 位于 ``PytranApp.run()`` 内部，长期被
``# pragma: no cover`` 保护，``tests/test_ui_settings.py`` 只覆盖抽离出的
``_apply_settings`` / ``_coerce_setting``——而 GUI 真实路径**从不调用**它们。
由此漏出三类缺陷：

* **A1**：``save()`` 用到 ``re.search``，但模块从未 ``import re``。
  触发条件是术语表存在「原文非空 + 译名为空」的行（即设计文档
  W5b §3.2 中「译名留空 = 按原文保留」那一行）——每行都填译名则不崩，
  所以常规操作路径下看起来正常。
* **A2**：``timeout`` / ``max_retry_rounds`` / ``context_lines`` /
  ``block_lines`` 未在 ``_coerce_setting`` 的类型表中，字符串直达 ``Config``。
  当次会话内 ``min("3", 5)`` 直接 ``TypeError``；重启后因
  ``Config.load`` 的 ``_coerce_types`` 自愈，所以极难被发现。
* **A3**：``save()`` 直接 ``setattr(cfg, ...)`` 落盘，绕过了
  ``_apply_settings``，测试覆盖的路与 GUI 真实路是两条。

本模块用 offscreen 平台构建真实窗口、打开真实设置对话框、点真实「保存」
按钮，锁定上述行为，防止复发。
"""

from __future__ import annotations

import configparser
import os

import pytest
pytest.importorskip("PyQt6", reason="设置对话框 GUI 级测试需要 PyQt6（GUI 依赖，可选）")

# 无显示环境（CI / 沙箱）下用 offscreen 平台，必须在 QApplication 之前设置。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QPushButton, QTableWidget, QTableWidgetItem,
)

from pytran.config import Config  # noqa: E402
from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover - 平台不支持时跳过
        pytest.skip(f"无法创建 QApplication：{exc}")
    return app


@pytest.fixture()
def gui(qt_app, monkeypatch, tmp_path):
    """构建真实窗口（不进入事件循环），返回 PytranApp。

    每个测试独立的 ``tmp_path`` 工作目录，避免设置写入互相污染
    （否则前一个测试改的 timeout 会被后一个测试读到）。
    """
    from PyQt6.QtWidgets import QMessageBox

    # run() 末尾会阻塞在 app.exec()；替换为立即返回来只验证构建。
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    # 设置对话框是模态的：dlg.exec() 会阻塞。换成立即返回，
    # 让测试可以在调用后继续操作对话框上的真实控件。
    monkeypatch.setattr(QDialog, "exec", lambda self: 0, raising=False)
    # save() 末尾的 QMessageBox.information 同样是模态阻塞，必须 patch，
    # 否则测试会永久挂住（这是无头驱动 GUI 的常见坑）。
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: None, raising=False)

    segments = [
        Segment(uid=1, file="a.rpy", locator="l1", feature="f1",
                kind="dialogue", comment="", role="", source="Hello there",
                status=SegmentStatus.PENDING),
    ]
    app = PytranApp(Project(segments=segments, game_root=""), work_dir=tmp_path)
    app._log = lambda *a, **k: None  # 静默日志，保持测试输出干净
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _current_config(gui: PytranApp) -> Config:
    """取当前生效的 Config（不重启口径：直接读内存对象）。"""
    if gui.deps is not None:
        return gui.deps.config
    return Config.load(gui.work_dir)


def _open_settings(gui: PytranApp) -> QDialog:
    """打开设置对话框并返回它（exec 已被 monkeypatch 成立即返回）。"""
    gui._open_settings()
    dlg = gui._settings_dialog
    assert dlg is not None, "_open_settings 应记录对话框引用供测试驱动"
    return dlg


def _terms_table(dlg: QDialog) -> QTableWidget:
    tables = dlg.findChildren(QTableWidget)
    assert tables, "设置对话框应包含术语表表格"
    return tables[0]


def _save_button(dlg: QDialog) -> QPushButton:
    btns = [b for b in dlg.findChildren(QPushButton) if b.text() == "保存"]
    assert btns, "设置对话框应有「保存」按钮"
    return btns[0]


# --------------------------------------------------------------------------- #
# A1：译名为空的行 → save() 不得抛 NameError
# --------------------------------------------------------------------------- #
def test_save_with_empty_translation_row_does_not_raise(gui: PytranApp) -> None:
    """A1 回归：术语表存在「原文有值 / 译名为空」的行时点保存不得崩。

    这正是设计文档 W5b §3.2 示例表格中 `Stalkfap` 那一行
    （译名留空 = 按原文保留）的操作路径。
    """
    dlg = _open_settings(gui)
    table = _terms_table(dlg)

    row = table.rowCount()
    table.insertRow(row)
    table.setItem(row, 0, QTableWidgetItem("Stalkfap"))
    # 第 1 列（译名）故意留空 → 命中 save() 里的 `elif re.search(...)` 分支

    # 不得抛 NameError（A1 修复前这里必崩）
    _save_button(dlg).click()


def test_save_pure_translation_rows_ok(gui: PytranApp) -> None:
    """对照组：每行都填译名时保存同样正常（A1 修复前这条路径也不崩）。"""
    dlg = _open_settings(gui)
    table = _terms_table(dlg)

    row = table.rowCount()
    table.insertRow(row)
    table.setItem(row, 0, QTableWidgetItem("Ian"))
    table.setItem(row, 1, QTableWidgetItem("伊恩"))

    _save_button(dlg).click()


# --------------------------------------------------------------------------- #
# A2：数值字段经对话框保存后，内存 Config 中类型必须正确
# --------------------------------------------------------------------------- #
def test_saved_numeric_fields_are_not_strings(gui: PytranApp) -> None:
    """A2 回归：timeout / max_retry_rounds / context_lines / block_lines
    经设置对话框保存后，在**不重启**的情况下也必须是 int / float。

    修复前 save() 直接 setattr，这四个字段（以及其它全部字段）都是字符串，
    会被原样传给 openai SDK 的 timeout 与 Segmenter 的 context_lines。
    """
    dlg = _open_settings(gui)

    # QLineEdit 无法按对象名查找（都是 _open_settings 里的局部变量），
    # 按当前值匹配。注意字段顺序（见 ui._open_settings 的 fields 清单）：
    #   [api_key, api_url, model, fallback_model, max_tokens, temperature,
    #    top_p, max_workers, timeout, max_retry_rounds, renpy_sdk_dir,
    #    language, context_lines, block_lines]
    from PyQt6.QtWidgets import QLineEdit
    lines = dlg.findChildren(QLineEdit)
    assert len(lines) == 14, f"设置页应有 14 个字段，实际 {len(lines)}"

    # 按下标取，避免同值字段（max_retry_rounds 与 context_lines 默认都是 3）歧义
    timeout_edit = lines[8]     # 默认 "120.0"
    retry_edit = lines[9]       # 默认 "3"
    ctx_edit = lines[12]        # 默认 "3"
    block_edit = lines[13]      # 默认 "30"
    assert float(timeout_edit.text()) == 120.0
    assert int(retry_edit.text()) == 3
    assert int(ctx_edit.text()) == 3
    assert int(block_edit.text()) == 30

    timeout_edit.setText("60")
    retry_edit.setText("2")
    ctx_edit.setText("5")
    block_edit.setText("20")
    _save_button(dlg).click()

    cfg = _current_config(gui)
    # 关键断言：不重启也必须是数值
    assert isinstance(cfg.timeout, (int, float)), \
        f"timeout 应为数值，实际 {type(cfg.timeout).__name__}"
    assert isinstance(cfg.max_retry_rounds, int), \
        f"max_retry_rounds 应为 int，实际 {type(cfg.max_retry_rounds).__name__}"
    assert isinstance(cfg.context_lines, int), \
        f"context_lines 应为 int，实际 {type(cfg.context_lines).__name__}"
    assert isinstance(cfg.block_lines, int), \
        f"block_lines 应为 int，实际 {type(cfg.block_lines).__name__}"
    # effective_max_retry_rounds 在字符串下会 TypeError
    assert cfg.effective_max_retry_rounds() == 2
    assert float(cfg.timeout) == 60.0
    assert cfg.context_lines == 5
    assert cfg.block_lines == 20


def test_save_empty_numeric_field_keeps_existing(gui: PytranApp) -> None:
    """A2 回归：数值字段清空时应保留既有值，不被写成空串。"""
    dlg = _open_settings(gui)
    from PyQt6.QtWidgets import QLineEdit
    lines = dlg.findChildren(QLineEdit)

    timeout_edit = lines[8]     # 默认 "120.0"
    timeout_edit.setText("")    # 清空 → 应保留 120.0
    _save_button(dlg).click()

    cfg = _current_config(gui)
    assert float(cfg.timeout) == 120.0


# --------------------------------------------------------------------------- #
# A3：save() 必须走 _apply_settings（而非裸 setattr）
# --------------------------------------------------------------------------- #
def test_save_routes_through_apply_settings(gui: PytranApp, monkeypatch) -> None:
    """A3 回归：对话框保存必须调用 ``_apply_settings``。

    修复前 save() 直接 ``setattr(cfg, ...)`` + ``cfg.save()``，
    ``_coerce_setting`` 的类型收敛被完全绕过。
    """
    dlg = _open_settings(gui)
    seen: dict = {}
    real = PytranApp._apply_settings

    def spy(self, values):
        seen["values"] = dict(values)
        return real(self, values)

    monkeypatch.setattr(PytranApp, "_apply_settings", spy)
    _save_button(dlg).click()

    assert "values" in seen, "save() 应通过 _apply_settings 写回配置"
    # 全部 14 个设置字段都应进 values
    for attr in ("api_key", "api_url", "model", "timeout",
                 "max_retry_rounds", "context_lines", "block_lines"):
        assert attr in seen["values"], f"{attr} 应出现在 _apply_settings 入参中"


# --------------------------------------------------------------------------- #
# B5：保存弹窗文案须与 ADR-0004 修订一致
# --------------------------------------------------------------------------- #
def test_save_message_matches_adr_0004(gui: PytranApp) -> None:
    """B5 回归：api_key 已改为落盘本机 ini（ADR-0004 2026-10-13 修订），
    弹窗不得再声称「不会写入文件」——那与 Tab1 标签自相矛盾。

    fixture 里已把 QMessageBox.information patch 成静默；这里换成捕获版。
    """
    from PyQt6.QtWidgets import QMessageBox

    captured: dict = {}
    gui._qt_messagebox_information = QMessageBox.information  # 便于还原
    QMessageBox.information = staticmethod(
        lambda *a, **k: captured.setdefault("text", a[2] if len(a) > 2 else k.get("text"))
    )
    try:
        dlg = _open_settings(gui)
        _save_button(dlg).click()
    finally:
        QMessageBox.information = gui._qt_messagebox_information

    text = captured.get("text", "")
    assert "不会写入文件" not in text, "弹窗仍声称 API Key 不落盘，与 ADR-0004 修订矛盾"
    assert "config.ini" in text


# --------------------------------------------------------------------------- #
# 对话框结构：block_lines 字段必须存在（9ee271f 遗漏项）
# --------------------------------------------------------------------------- #
def test_block_lines_field_present_in_settings(gui: PytranApp) -> None:
    """9ee271f 给 config/engine/Segmenter 接了 block_lines，却漏了设置页字段，
    导致该参数在 GUI 里无法配置。此处锁定字段存在。"""
    dlg = _open_settings(gui)
    from PyQt6.QtWidgets import QLineEdit
    lines = dlg.findChildren(QLineEdit)
    assert len(lines) == 14
    # 第 14 个字段就是 block_lines，默认 30
    assert lines[13].text() == "30", \
        "设置页应有「切块大小 (block_lines)」字段（默认 30）"
