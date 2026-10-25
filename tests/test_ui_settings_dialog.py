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
    #    language, context_lines, block_lines,
    #    review_model, review_window_lines, review_max_tokens]
    # review_enabled 是 QCheckBox，不在 QLineEdit 计数内。
    from PyQt6.QtWidgets import QLineEdit
    lines = dlg.findChildren(QLineEdit)
    assert len(lines) == 17, f"设置页应有 17 个文本字段，实际 {len(lines)}"

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
    assert len(lines) == 17
    # 第 14 个字段就是 block_lines，默认 30
    assert lines[13].text() == "30", \
        "设置页应有「切块大小 (block_lines)」字段（默认 30）"


# --------------------------------------------------------------------------- #
# 复核阶段字段（AI 校对，取代已删除的 V7 批次指纹检测）：窗口/模型必须可调
# --------------------------------------------------------------------------- #
def test_review_fields_present_and_defaulted(gui: PytranApp) -> None:
    """复核窗口行数与复核模型必须能在设置页调，且默认值来自 config 模板。

    用户要求：窗口大小与复核模型都要在设置界面可调，默认值取
    ``DeepSeek-R1-0528-Qwen3-8B`` 的上下文所能允许的量。
    """
    dlg = _open_settings(gui)
    from PyQt6.QtWidgets import QLineEdit
    lines = dlg.findChildren(QLineEdit)
    # 字段顺序见 _open_settings 的 fields 清单（末尾三个）
    assert lines[14].text() == "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", \
        "复核模型字段应默认 DeepSeek-R1-0528-Qwen3-8B"
    assert lines[15].text() == "2000", "复核窗口行数字段应默认 2000"
    assert lines[16].text() == "8192", "复核输出上限字段应默认 8192"


def test_review_window_and_model_are_saved_as_numeric(gui: PytranApp) -> None:
    """复核窗口行数经保存后必须是 int（否则 chunk 循环会 TypeError）。"""
    dlg = _open_settings(gui)
    from PyQt6.QtWidgets import QLineEdit
    lines = dlg.findChildren(QLineEdit)
    lines[14].setText("other/review-model")
    lines[15].setText("500")
    lines[16].setText("4096")
    _save_button(dlg).click()

    cfg = _current_config(gui)
    assert cfg.review_model == "other/review-model"
    assert isinstance(cfg.review_window_lines, int), \
        f"review_window_lines 应为 int，实际 {type(cfg.review_window_lines).__name__}"
    assert isinstance(cfg.review_max_tokens, int)
    assert cfg.review_window_lines == 500
    assert cfg.review_max_tokens == 4096


def test_review_enabled_checkbox_round_trips(gui: PytranApp) -> None:
    """复核开关用 QCheckBox：取消勾选后必须落成 False（不是字符串 "False"）。"""
    dlg = _open_settings(gui)
    from PyQt6.QtWidgets import QCheckBox
    boxes = [b for b in dlg.findChildren(QCheckBox)
             if "复核" in b.text()]
    assert boxes, "设置页应有复核开关勾选框"
    cb = boxes[0]
    assert cb.isChecked() is True, "复核开关默认应为开"

    cb.setChecked(False)
    _save_button(dlg).click()

    cfg = _current_config(gui)
    assert cfg.review_enabled is False, \
        f"review_enabled 应为 False，实际 {cfg.review_enabled!r}"
    # 落盘后重新读取也必须还是 False（ini 往返）
    assert Config.load(gui.work_dir).review_enabled is False


def test_review_window_empty_keeps_existing(gui: PytranApp) -> None:
    """复核窗口留空 → 保留既有值，不被写成空串。"""
    dlg = _open_settings(gui)
    from PyQt6.QtWidgets import QLineEdit
    lines = dlg.findChildren(QLineEdit)
    lines[15].setText("")
    _save_button(dlg).click()

    cfg = _current_config(gui)
    assert cfg.review_window_lines == 2000


# --------------------------------------------------------------------------- #
# 持久化落盘（2026-10-25 事故后续）：术语表 / 翻译规则 / 免翻短语都必须有
# 用户可读镜像，且保存后立刻同步——不能只有 profile.json 一份。
# --------------------------------------------------------------------------- #
def _rules_edit(dlg: QDialog):
    from PyQt6.QtWidgets import QPlainTextEdit
    edit = dlg.findChild(QPlainTextEdit, "rules_edit")
    assert edit is not None, "设置对话框应有 objectName=rules_edit 的翻译规则框"
    return edit


def test_save_writes_rules_mirror(gui: PytranApp) -> None:
    """翻译规则保存后必须落一份 rules.txt（事故里规则是唯一无副本的资产）。"""
    dlg = _open_settings(gui)
    _rules_edit(dlg).setPlainText("保持人名不译，语气口语化。")
    _save_button(dlg).click()

    rules_file = gui.work_dir / "rules.txt"
    assert rules_file.is_file(), "保存翻译规则后应生成 rules.txt"
    assert rules_file.read_text(encoding="utf-8-sig").strip() == "保持人名不译，语气口语化。"

    from pytran.profile_store import read_rules
    assert read_rules(gui.work_dir) == "保持人名不译，语气口语化。"


def test_save_writes_glossary_and_exempt_mirrors(gui: PytranApp) -> None:
    """术语表与免翻短语保存后也必须有镜像（glossary.csv / exempt.txt）。"""
    dlg = _open_settings(gui)
    table = _terms_table(dlg)
    table.setRowCount(1)
    table.setItem(0, 0, QTableWidgetItem("Ian"))
    table.setItem(0, 1, QTableWidgetItem("伊恩"))
    _save_button(dlg).click()

    assert (gui.work_dir / "glossary.csv").is_file()
    csv_text = (gui.work_dir / "glossary.csv").read_text(encoding="utf-8-sig")
    assert "Ian" in csv_text and "伊恩" in csv_text


def test_rules_survive_profile_json_loss(gui: PytranApp) -> None:
    """端到端：保存规则 → 删掉 profile.json → 重新加载，规则必须还在。"""
    dlg = _open_settings(gui)
    _rules_edit(dlg).setPlainText("专有名词一律音译。")
    _save_button(dlg).click()

    (gui.work_dir / "profile.json").unlink()
    reloaded = gui._load_profile()
    assert reloaded.prompt_overrides.get("system_append") == "专有名词一律音译。", \
        "profile.json 丢失后翻译规则应从 rules.txt 恢复"


def test_rules_export_button_writes_mirror_without_saving(gui: PytranApp) -> None:
    """「导出到 rules.txt」按钮：不点保存也能立刻落盘（用户手工留底用）。"""
    dlg = _open_settings(gui)
    _rules_edit(dlg).setPlainText("临时草稿规则")
    btn = next(b for b in dlg.findChildren(QPushButton)
               if "rules.txt" in b.text() and "导出" in b.text())
    btn.click()

    from pytran.profile_store import read_rules
    assert read_rules(gui.work_dir) == "临时草稿规则"
