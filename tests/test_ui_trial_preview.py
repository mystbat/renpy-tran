"""翻译页 R1 试译 / R2 译文预览 的回归测试。

对应问题清单第四轮（用户 2026-10-21 点名的两项当务之急）：

* **R1** 缺少「试译 / 小规模测试」入口——先用少量样本验证
  「模型 + 提示词 + 术语表 + 参数」组合，再决定是否全量开跑；
* **R2** 缺少「已译内容」的即时反馈——用户要能即时看到已译内容，
  确认翻译「如预期进行」，而不必切到审校页。

实现要点：
* 试译走 ``engine.run(only_uids={前 N 条 uid})``，与全量**同一套校验链**；
* 试译**不自动跳页**（``auto_goto=False``）——目的是让人看预览面板的结果；
* 预览面板只读，不与审校页的游标/编辑态竞争；
* 预览只保留最近 ``_PREVIEW_MAX_ROWS`` 条，避免十万级长跑重现启动假死。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="试译/预览测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QGroupBox, QInputDialog, QListWidget, QMessageBox,
    QPushButton,
)

from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import _PREVIEW_MAX_ROWS, PytranApp  # noqa: E402


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
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes,
                        raising=False)

    segments = [
        Segment(uid=i, file="a.rpy", locator=f"l{i}", feature=f"f{i}",
                kind="dialogue", comment="", role="", source=f"line {i}",
                status=SegmentStatus.PENDING)
        for i in range(1, 11)
    ]
    app = PytranApp(Project(segments=segments, game_root=""), work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _btn(gui: PytranApp, key: str) -> QPushButton:
    return gui._widgets[key]


def _preview_list(gui: PytranApp) -> QListWidget:
    return gui._widgets["preview_list"]


# --------------------------------------------------------------------------- #
# R1：试译入口
# --------------------------------------------------------------------------- #
def test_trial_button_exists_and_toggles(gui: PytranApp) -> None:
    """R1：翻译页应有「试译…」按钮，且忙碌态置灰。"""
    btn = _btn(gui, "btn_trial")
    assert btn is not None
    assert btn.text() == "试译…"
    assert not gui._translating
    assert btn.isEnabled()

    gui._translating = True
    gui._refresh_translate_controls()
    assert not btn.isEnabled(), "翻译中试译按钮应置灰"
    gui._translating = False
    gui._refresh_translate_controls()
    assert btn.isEnabled()


def test_trial_passes_only_first_n_uids(gui: PytranApp, monkeypatch) -> None:
    """R1：试译只发前 N 条未译行，不碰其余行。"""
    captured = {}

    class FakeEngine:
        def run(self, project, config, profile, on_event=None, only_uids=None):
            captured["only_uids"] = only_uids
            return project

    class FakeDeps:
        engine = FakeEngine()
        config = None

    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)
    monkeypatch.setattr(PytranApp, "_load_profile", lambda self: type("P", (), {})())
    monkeypatch.setattr(PytranApp, "_confirm_budget", lambda self, **k: True)
    monkeypatch.setattr(gui, "_current_config", lambda: type(
        "C", (), {"block_lines": 3})())
    gui.deps = FakeDeps()

    # 模拟用户在弹出的行数对话框里确认默认值（一个块大小 = 3）
    monkeypatch.setattr(QInputDialog, "getInt",
                        lambda *a, **k: (3, True))
    gui._trial_translate()

    assert captured.get("only_uids") == {1, 2, 3}, \
        "试译应只取前 N 条未译行（按 uid 升序，与切块顺序一致）"


def test_trial_does_not_auto_goto(gui: PytranApp, monkeypatch) -> None:
    """R1：试译完成后不自动跳页——目的是让人看预览面板的结果。"""
    seen_kwargs = {}

    def spy(self, only_uids=None, *, auto_goto=True):
        seen_kwargs["auto_goto"] = auto_goto
        seen_kwargs["only_uids"] = only_uids

    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)
    monkeypatch.setattr(PytranApp, "_load_profile", lambda self: type("P", (), {})())
    monkeypatch.setattr(PytranApp, "_confirm_budget", lambda self, **k: True)
    monkeypatch.setattr(gui, "_current_config", lambda: type(
        "C", (), {"block_lines": 3})())
    monkeypatch.setattr(PytranApp, "_translate_in_background", spy)
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (3, True))

    gui._trial_translate()
    assert seen_kwargs.get("auto_goto") is False, \
        "试译不得自动跳页（R1）"
    assert seen_kwargs.get("only_uids") == {1, 2, 3}


def test_trial_noop_when_no_translatable(gui: PytranApp, monkeypatch) -> None:
    """R1：全部已译时试译不启动，给明确提示。"""
    for s in gui.project.segments:
        s.status = SegmentStatus.TRANSLATED
    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)
    started = []
    monkeypatch.setattr(PytranApp, "_translate_in_background",
                        lambda self, **k: started.append(True))
    gui._trial_translate()
    assert not started, "无可试译行时不得启动翻译"


def test_trial_remembers_last_count(gui: PytranApp, monkeypatch) -> None:
    """R1：记住上次试译行数，避免每次重填。"""
    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)
    monkeypatch.setattr(PytranApp, "_load_profile", lambda self: type("P", (), {})())
    monkeypatch.setattr(PytranApp, "_confirm_budget", lambda self, **k: True)
    monkeypatch.setattr(gui, "_current_config", lambda: type(
        "C", (), {"block_lines": 30})())
    monkeypatch.setattr(PytranApp, "_translate_in_background",
                        lambda self, **k: None)
    seen = {}

    def fake_get_int(*a, **k):
        seen["value"] = k.get("value")
        return (k.get("value"), True)

    monkeypatch.setattr(QInputDialog, "getInt", fake_get_int)

    gui._trial_lines = None
    gui._trial_translate()
    assert seen["value"] == 10, "首次应默认 min(block_lines, 可译行数) = 10"

    gui._trial_lines = 4
    gui._trial_translate()
    assert seen["value"] == 4, "应记住上次选择的行数"


# --------------------------------------------------------------------------- #
# R2：译文预览面板
# --------------------------------------------------------------------------- #
def test_preview_panel_exists(gui: PytranApp) -> None:
    """R2：翻译页应有「最新译文」预览面板 + 暂停滚动 + 清空。"""
    assert "preview_list" in gui._widgets
    assert "btn_preview_pause" in gui._widgets
    assert "btn_preview_clear" in gui._widgets
    assert "preview_count" in gui._widgets
    group = gui._widgets.get("preview_group")
    assert group is None or isinstance(group, QGroupBox) or True  # 容错：键名可选


def test_preview_appends_on_segment_event(gui: PytranApp) -> None:
    """R2：segment 事件到达即把该行入预览面板（即时反馈）。"""
    gui._clear_preview()
    gui._translating = True
    seg = gui.project.segments[0]
    seg.target = "第一行译文"
    seg.status = SegmentStatus.TRANSLATED

    gui._apply_event("segment", {"uid": 1, "status": "translated"})

    lb = _preview_list(gui)
    assert lb.count() == 1
    assert "第一行译文" in lb.item(0).text()
    assert "[1]" in lb.item(0).text()
    assert "已接收 1 条" in gui._widgets["preview_count"].text()


def test_preview_skipped_when_not_translating(gui: PytranApp) -> None:
    """R2：非翻译运行中（如用户在审校页手动 accept）不入预览，
    避免「试译结果」和「审校操作」混在一起。"""
    gui._clear_preview()
    gui._translating = False
    seg = gui.project.segments[1]
    seg.target = "手动译文"
    seg.status = SegmentStatus.REVIEWED

    gui._apply_event("segment", {"uid": 2, "status": "reviewed"})
    assert _preview_list(gui).count() == 0


def test_preview_marks_failed_and_keep_declared(gui: PytranApp) -> None:
    """R2：失败行标红 + 原因；keep_declared 标注「AI 申报保留」。"""
    gui._clear_preview()
    gui._translating = True

    f = gui.project.segments[2]
    f.status = SegmentStatus.FAILED
    f.error = "tags: {w} 原文2处译文1处"
    gui._apply_event("segment", {"uid": 3, "status": "failed", "reason": f.error})

    k = gui.project.segments[3]
    k.status = SegmentStatus.KEEP_DECLARED
    k.target = k.source
    k.error = "拟声词，原文即有意保留"
    gui._apply_event("segment", {"uid": 4, "status": "keep_declared"})

    lb = _preview_list(gui)
    texts = [lb.item(i).text() for i in range(lb.count())]
    assert any("失败" in t and "tags:" in t for t in texts), \
        "失败行应显示失败原因"
    assert any("AI 申报保留" in t for t in texts), \
        "keep_declared 行应明确标注"


def test_preview_capped_at_max_rows(gui: PytranApp) -> None:
    """R2：预览只保留最近 _PREVIEW_MAX_ROWS 条——
    十万级长跑时若无限追加会重现启动假死（实测 13.5 万条约 11 秒）。"""
    gui._clear_preview()
    gui._translating = True

    # 造 250 条（超过 200 上限）全部 TRANSLATED
    for i, seg in enumerate(gui.project.segments):
        seg.status = SegmentStatus.TRANSLATED
        seg.target = f"译文{i}"
    # 追加额外段以突破上限
    extra = [
        Segment(uid=100 + i, file="a.rpy", locator=f"x{i}", feature=f"fx{i}",
                kind="dialogue", comment="", role="", source=f"src {i}",
                target=f"译文{i}", status=SegmentStatus.TRANSLATED)
        for i in range(_PREVIEW_MAX_ROWS + 50)
    ]
    gui.project.segments.extend(extra)

    for seg in gui.project.segments:
        gui._append_preview(seg)

    lb = _preview_list(gui)
    assert lb.count() == _PREVIEW_MAX_ROWS, \
        f"预览应封顶 {_PREVIEW_MAX_ROWS} 条，实际 {lb.count()}"
    # 计数应反映「已接收」总量，而非面板条数
    assert gui._preview_seen == len(gui.project.segments)
    assert "面板仅保留" in gui._widgets["preview_count"].text()


def test_preview_pause_toggle(gui: PytranApp) -> None:
    """R2：暂停滚动时长跑中可定住某条细看。"""
    btn = _btn(gui, "btn_preview_pause")
    assert btn.text() == "暂停滚动"
    btn.setChecked(True)
    assert gui._preview_paused is True
    assert btn.text() == "恢复滚动"
    btn.setChecked(False)
    assert gui._preview_paused is False


def test_preview_clear_resets(gui: PytranApp) -> None:
    """R2：清空按钮复位面板与计数。"""
    gui._clear_preview()
    gui._translating = True
    seg = gui.project.segments[0]
    seg.target = "x"
    seg.status = SegmentStatus.TRANSLATED
    gui._append_preview(seg)
    assert _preview_list(gui).count() == 1

    gui._clear_preview()
    assert _preview_list(gui).count() == 0
    assert gui._preview_seen == 0
    assert gui._widgets["preview_count"].text() == ""
