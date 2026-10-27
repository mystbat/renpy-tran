"""FEATURE(fallback-thinking)：回退模型专用思维链开关。

用户要求：「给回退模型设置思维链开关」。
主模型 ``enable_thinking`` 与回退模型 ``fallback_enable_thinking`` 互相独立，
请求按**本次实际使用的模型**取对应开关。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pytran.config import Config, _DEFAULT_INI
from pytran.providers import OpenAIProvider
from pytran.terms import TermProfile


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))])


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def _provider(responses, **cfg_kw):
    client = FakeClient(responses)
    cfg = Config(api_key="sk-test-key", model="m1", **cfg_kw)
    return OpenAIProvider(cfg, client=client, sleep=lambda _s: None), client


def _extra_body(client, idx=0):
    return client.chat.completions.calls[idx].get("extra_body")


# --------------------------------------------------------------------------- #
# 配置层
# --------------------------------------------------------------------------- #
def test_defaults_to_true():
    """默认 True = 允许思维链，与改造前回退请求逐字节一致。"""
    assert Config().fallback_enable_thinking is True


def test_default_ini_template_has_key():
    """默认模板必须带该键，否则首次运行的用户看不到也改不了。"""
    assert "fallback_enable_thinking = true" in _DEFAULT_INI


def test_roundtrips_through_ini(tmp_path: Path):
    """存 false → 重新加载仍为 False（不能退化成字符串 "False" 的真值）。"""
    cfg = Config(api_key="k", model="m", fallback_model="fb",
                 fallback_enable_thinking=False, work_dir=str(tmp_path))
    cfg.save(tmp_path)
    assert Config.load(tmp_path).fallback_enable_thinking is False


def test_missing_key_defaults_to_true(tmp_path: Path):
    """旧 config.ini（无该键）→ True，升级后行为不变。"""
    ini = tmp_path / "config.ini"
    ini.write_text("[Model]\nmodel = m1\nfallback_model = fb\n", encoding="utf-8")
    assert Config.load(tmp_path).fallback_enable_thinking is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("false", False), ("1", True), ("0", False),
    ("yes", True), ("no", False), ("on", True), ("off", False),
])
def test_accepts_configparser_booleans(tmp_path: Path, raw, expected):
    ini = tmp_path / "config.ini"
    ini.write_text(
        "[Model]\nmodel = m1\nfallback_model = fb\n"
        "fallback_enable_thinking = %s\n" % raw, encoding="utf-8")
    assert Config.load(tmp_path).fallback_enable_thinking is expected


def test_independent_of_main_switch():
    """两个字段必须能各自独立取值，不能是同一个属性。"""
    cfg = Config(api_key="k", model="m", fallback_model="fb",
                 enable_thinking=False, fallback_enable_thinking=True)
    assert cfg.enable_thinking is False
    assert cfg.fallback_enable_thinking is True


# --------------------------------------------------------------------------- #
# Provider 层：按实际使用的模型选开关
# --------------------------------------------------------------------------- #
def test_main_model_uses_main_switch():
    """走主模型时，主开关关 → 发 extra_body。"""
    provider, client = _provider(
        ['{"i": 1, "t": "你好"}'],
        enable_thinking=False, fallback_enable_thinking=True, fallback_model="fb")
    provider.translate_batch([(1, "Hello")], TermProfile())
    assert _extra_body(client) == {"enable_thinking": False}


def test_fallback_model_uses_fallback_switch():
    """切到回退模型时，回退开关关 → 发 extra_body（即使主开关是开的）。"""
    provider, client = _provider(
        ['{"i": 1, "t": "你好"}'],
        enable_thinking=True, fallback_enable_thinking=False, fallback_model="fb")
    provider.translate_batch([(1, "Hello")], TermProfile(), use_fallback_model=True)
    assert _extra_body(client) == {"enable_thinking": False}


def test_fallback_model_keeps_thinking_when_fallback_switch_on():
    """回退开关开 → 不发参数，即使主开关关了。这是两个开关独立的核心保证。"""
    provider, client = _provider(
        ['{"i": 1, "t": "你好"}'],
        enable_thinking=False, fallback_enable_thinking=True, fallback_model="fb")
    provider.translate_batch([(1, "Hello")], TermProfile(), use_fallback_model=True)
    assert "extra_body" not in client.chat.completions.calls[0]


def test_both_off_sends_extra_body_on_either_model():
    provider, client = _provider(
        ['{"i": 1, "t": "a"}', '{"i": 1, "t": "b"}'],
        enable_thinking=False, fallback_enable_thinking=False, fallback_model="fb")
    provider.translate_batch([(1, "Hello")], TermProfile())
    provider.translate_batch([(1, "Hello")], TermProfile(), use_fallback_model=True)
    assert _extra_body(client, 0) == {"enable_thinking": False}
    assert _extra_body(client, 1) == {"enable_thinking": False}


def test_fallback_switch_ignored_when_no_fallback_model_configured():
    """`use_fallback_model=True` 但没配回退模型时，实际仍走主模型
    （见 protocol.build_request_params），故必须用主开关——否则会出现
    「设置里关掉了主模型思维链，却因为标志位误发/漏发」的不一致。"""
    provider, client = _provider(
        ['{"i": 1, "t": "你好"}'],
        enable_thinking=False, fallback_enable_thinking=True, fallback_model="")
    provider.translate_batch([(1, "Hello")], TermProfile(), use_fallback_model=True)
    # 实际模型仍是 m1，故应遵循主开关（关）→ 发 extra_body
    assert client.chat.completions.calls[0]["model"] == "m1"
    assert _extra_body(client) == {"enable_thinking": False}


def test_missing_attr_falls_back_to_true():
    """config 对象没有该属性时（测试替身/旧对象）按 True 处理，不抛异常。"""
    provider, client = _provider(
        ['{"i": 1, "t": "你好"}'],
        enable_thinking=True, fallback_model="fb")
    del provider._config.fallback_enable_thinking
    provider.translate_batch([(1, "Hello")], TermProfile(), use_fallback_model=True)
    assert "extra_body" not in client.chat.completions.calls[0]


# --------------------------------------------------------------------------- #
# UI 层：勾选框存在、默认勾选、落盘为 bool
# --------------------------------------------------------------------------- #
def test_settings_checkbox_exists_and_defaults_checked(tmp_path: Path):
    """设置页必须有「回退模型允许思维链」勾选框，默认勾选（=允许）。"""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog
    import pytran.ui as ui
    from pytran.models import Project

    app = QApplication.instance() or QApplication([])
    orig_app, orig_dlg = QApplication.exec, QDialog.exec
    QApplication.exec = lambda self: 0
    QDialog.exec = lambda self: 0
    try:
        g = ui.PytranApp(Project(segments=[]), work_dir=str(tmp_path))
        g._open_settings()
        dlg = g._settings_dialog
        cb = dlg.findChild(QCheckBox, "cb_fallback_enable_thinking")
        assert cb is not None, "设置页应有 objectName=cb_fallback_enable_thinking 的勾选框"
        assert cb.isChecked() is True, "默认应勾选（允许思维链）"
        assert "回退模型" in cb.text()
        assert cb.toolTip(), "勾选框应有说明性 tooltip"
    finally:
        QApplication.exec, QDialog.exec = orig_app, orig_dlg


def test_settings_checkbox_saves_as_bool(tmp_path: Path):
    """取消勾选后必须落成 bool False（不是字符串 "False"）。"""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QPushButton, QMessageBox
    import pytran.ui as ui
    from pytran.models import Project

    app = QApplication.instance() or QApplication([])
    orig_app, orig_dlg = QApplication.exec, QDialog.exec
    orig_info = QMessageBox.information
    QApplication.exec = lambda self: 0
    QDialog.exec = lambda self: 0
    QMessageBox.information = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Ok)
    try:
        g = ui.PytranApp(Project(segments=[]), work_dir=str(tmp_path))
        g._open_settings()
        dlg = g._settings_dialog
        dlg.findChild(QCheckBox, "cb_fallback_enable_thinking").setChecked(False)
        btn = [b for b in dlg.findChildren(QPushButton)
               if b.text().strip() == "保存"][0]
        btn.click()

        loaded = Config.load(tmp_path)
        assert loaded.fallback_enable_thinking is False, \
            f"应落盘为 False，实际 {loaded.fallback_enable_thinking!r}"
        assert not isinstance(loaded.fallback_enable_thinking, str), \
            "不能存成字符串"
    finally:
        QApplication.exec, QDialog.exec = orig_app, orig_dlg
        QMessageBox.information = orig_info
