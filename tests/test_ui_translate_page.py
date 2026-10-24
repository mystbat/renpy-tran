"""翻译页 P1 修复的回归测试。

覆盖问题清单（``docs/翻译选项卡问题清单.md``）与 ``9ee271f``「翻译环节五项
UX 修复」中未接进 UI 的部分：

* **C1**：``round`` 事件把进度条打成 indeterminate 后，``_refresh_progress``
  从不恢复 ``setRange(0, 100)``，导致百分比永久失效；
* **C2**：空工程时 ``total or 1`` 把 0/0 掩饰成 0/1；
* **9ee271f 断线 1**：``block_lines`` 已接入 config/engine/Segmenter，
  但设置页缺字段 → GUI 里无法配置；
* **9ee271f 断线 2**：引擎发 ``chunk_done`` 块级事件，``_apply_event``
  没有分支 → 进度永远不按块前进；
* **9ee271f 断线 3**：``engine.stop()`` 已实现，UI 无「停止」按钮；
* **B1**：「重试失败项」与「开始自动翻译」共用同一回调；
* **A6**：§15.3 成本闸在 GUI 侧缺失；
* **C3/C4**：worker 线程直接 ``state.goto(3)`` 违反 §9「后台线程只发事件」。

PyQt6 属于可选的 GUI 依赖：未安装时整体跳过。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="翻译页测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QMessageBox, QPushButton,
)

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
    """构建真实窗口；阻塞式 API（exec / 模态对话框）全部 patch 成立即返回。"""
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
                status=SegmentStatus.PENDING if i < 8 else SegmentStatus.FAILED)
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


def _progress_bar(gui: PytranApp):
    return gui._widgets["progress"]


# --------------------------------------------------------------------------- #
# C1：round 事件后进度条必须还能显示百分比
# --------------------------------------------------------------------------- #
def test_round_event_does_not_permanently_disable_percentage(gui: PytranApp) -> None:
    """C1 回归：round 后进度条进入 busy，但 progress 事件必须能恢复百分比。"""
    pb = _progress_bar(gui)
    gui._apply_event("round", {"round": 2, "queue": 5})
    # round 期间允许 indeterminate
    assert pb.maximum() == 0, "round 期间进度条应为 busy"

    # 随后 progress 事件必须把 range 恢复成 0..100，否则 setValue 永久失效
    gui._apply_event("progress", {})
    assert pb.maximum() == 100, \
        "progress 事件后进度条必须恢复确定性模式（C1：否则百分比永远不显示）"
    # 8/10 已 FAILED → done=0；但至少 setValue 生效
    assert pb.value() == 0


def test_chunk_done_event_sets_percentage(gui: PytranApp) -> None:
    """9ee271f 断线 2：chunk_done 事件必须驱动进度条按块前进。"""
    pb = _progress_bar(gui)
    gui._apply_event("chunk_done", {"done": 3, "total": 10, "rows": 30})
    assert pb.maximum() == 100
    assert pb.value() == 30, "3/10 块完成应显示 30%"
    assert "3/10" in gui._widgets["progress_label"].text()

    gui._apply_event("chunk_done", {"done": 10, "total": 10, "rows": 30})
    assert pb.value() == 100


# --------------------------------------------------------------------------- #
# C2：空工程显示 0/0
# --------------------------------------------------------------------------- #
def test_empty_project_progress_shows_zero_of_zero(gui: PytranApp) -> None:
    """C2 回归：空工程不得被 ``total or 1`` 掩饰成 0/1。"""
    gui.project.segments.clear()
    gui._refresh_progress()
    assert gui._widgets["progress_label"].text() == "完成 0/0  失败 0"
    assert gui._widgets["progress"].value() == 0


# --------------------------------------------------------------------------- #
# 9ee271f 断线 1：block_lines 设置字段
# --------------------------------------------------------------------------- #
def test_block_lines_setting_present(gui: PytranApp) -> None:
    """9ee271f 断线 1：block_lines 必须可在 GUI 配置。"""
    from PyQt6.QtWidgets import QLineEdit
    gui._open_settings()
    lines = gui._settings_dialog.findChildren(QLineEdit)
    assert len(lines) == 14
    assert lines[13].text() == "30"


# --------------------------------------------------------------------------- #
# 9ee271f 断线 3：停止按钮
# --------------------------------------------------------------------------- #
def test_stop_button_exists_and_toggles_with_translating(gui: PytranApp) -> None:
    """9ee271f 断线 3：UI 必须有「停止」按钮，且只在翻译中可用。"""
    btn = gui._widgets.get("btn_stop")
    assert btn is not None, "翻译页应有「■ 停止」按钮"
    assert not btn.isEnabled(), "空闲时停止按钮应置灰"

    gui._translating = True
    gui._refresh_translate_controls()
    assert btn.isEnabled(), "翻译中停止按钮应可用"
    assert not gui._widgets["btn_translate"].isEnabled(), "翻译中「开始」应置灰"

    gui._translating = False
    gui._refresh_translate_controls()
    assert not btn.isEnabled()
    assert gui._widgets["btn_translate"].isEnabled()


def test_stop_calls_engine_stop(gui: PytranApp) -> None:
    """点「停止」应调用 engine.stop()（协作式中断）。"""
    calls = []

    class FakeEngine:
        def stop(self):
            calls.append(True)

    class FakeDeps:
        engine = FakeEngine()

    gui.deps = FakeDeps()
    gui._translating = True
    gui._stop_translate()
    assert calls == [True], "停止按钮应调用 engine.stop()"
    assert "停止" in gui._widgets["progress_label"].text()


def test_stop_without_engine_is_safe(gui: PytranApp) -> None:
    """deps/engine 为 None 时点停止不得崩。"""
    gui.deps = None
    gui._stop_translate()  # 不得抛


# --------------------------------------------------------------------------- #
# B1：重试失败项只处理失败行
# --------------------------------------------------------------------------- #
def test_retry_failed_passes_only_failed_uids(gui: PytranApp, monkeypatch) -> None:
    """B1 回归：「重试失败项」必须只把 FAILED 行交给引擎。"""
    captured = {}

    class FakeEngine:
        def run(self, project, config, profile, on_event=None, only_uids=None):
            captured["only_uids"] = only_uids
            return project

        def stop(self):
            pass

    class FakeDeps:
        engine = FakeEngine()
        config = None

    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)
    monkeypatch.setattr(PytranApp, "_load_profile",
                        lambda self: type("P", (), {})())
    monkeypatch.setattr(PytranApp, "_confirm_budget", lambda self, **k: True)
    gui.deps = FakeDeps()
    gui._retry_failed()

    expected = {s.uid for s in gui.project.segments
                if s.status == SegmentStatus.FAILED}
    assert captured.get("only_uids") == expected, \
        "重试失败项应只传 FAILED 行的 uid"


def test_retry_failed_noop_when_no_failures(gui: PytranApp, monkeypatch) -> None:
    """没有失败项时不得启动翻译（避免误导）。"""
    for s in gui.project.segments:
        s.status = SegmentStatus.PENDING
    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)
    started = []
    monkeypatch.setattr(PytranApp, "_translate_in_background",
                        lambda self, **k: started.append(True))
    gui._retry_failed()
    assert not started, "无失败项时不应启动翻译"


def test_retry_failed_works_after_a_previous_stop(
        gui: PytranApp, monkeypatch, tmp_path) -> None:
    """BUGFIX(stop-flag-sticky) 回归：点过一次「停止」后，「重试失败项」必须真的跑。

    用户实测：清空工程、重新导入 script.rpy 后点翻译 → 超时失败；
    再点「重试失败项」，日志立刻出现
    「重试 206 个失败项…… / 翻译已被用户停止」，一行都没翻。

    根因：``_ensure_engine_ready`` 复用同一个 engine 实例，而
    ``TranslationEngine._stop_requested`` 被 ``stop()`` 置 True 后再无人复位，
    于是后续每次 ``run()`` 都在第一个循环检查处退出。
    """
    import time

    from pytran.engine import TranslationEngine
    from pytran.validators import Validator

    calls = []

    class GoodProvider:
        def translate_batch(self, numbered_lines, profile, context=None,
                            allow_keep_declare=False, use_fallback_model=False):
            calls.append(len(numbered_lines))
            return {uid: f"译文{uid}" for uid, _src in numbered_lines}

    engine = TranslationEngine(GoodProvider(), Validator())
    engine.stop()  # 模拟「上一次运行被用户停止」留下的粘连标志
    assert engine._stop_requested is True

    class FakeDeps:
        pass

    deps = FakeDeps()
    deps.engine = engine
    deps.config = _config_for(tmp_path)
    gui.deps = deps

    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)
    monkeypatch.setattr(PytranApp, "_load_profile",
                        lambda self: type("P", (), {})())
    monkeypatch.setattr(PytranApp, "_confirm_budget", lambda self, **k: True)

    gui._retry_failed()

    # worker 在后台线程里跑，等它真正发出请求（最多 10s）。
    # 不依赖事件循环消费 translate_finished，只看 provider 是否被调用。
    deadline = time.time() + 10
    while time.time() < deadline and not calls:
        time.sleep(0.02)

    assert calls, "「重试失败项」必须真的发出翻译请求（不得被上次的停止标志拦下）"
    assert engine._stop_requested is False, "运行结束后停止标志应已复位"
    labels = gui._widgets["progress_label"].text()
    assert "已停止" not in labels, f"不应进入停止终态，实际：{labels}"


def _config_for(tmp_path):
    from pytran.config import Config
    return Config(work_dir=str(tmp_path), max_workers=1, block_lines=10,
                  context_lines=0, max_retry_rounds=0)


# --------------------------------------------------------------------------- #
# A6：成本闸
# --------------------------------------------------------------------------- #
def test_budget_gate_blocks_when_over_limit(gui: PytranApp, monkeypatch) -> None:
    """A6 回归：估算超预算时必须询问，用户拒绝则不开跑。"""
    monkeypatch.setattr(PytranApp, "_estimate_tokens",
                        lambda self, only_uids=None: 10_000)
    gui._budget_max = 100
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No,
                        raising=False)
    assert gui._confirm_budget() is False, "超预算且用户拒绝 → 不得开跑"

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes,
                        raising=False)
    assert gui._confirm_budget() is True, "用户确认 → 允许开跑"


def test_budget_gate_passes_when_within_limit(gui: PytranApp, monkeypatch) -> None:
    monkeypatch.setattr(PytranApp, "_estimate_tokens",
                        lambda self, only_uids=None: 50)
    gui._budget_max = 100
    assert gui._confirm_budget() is True


def test_budget_warning_label_shown_when_over(gui: PytranApp, monkeypatch) -> None:
    """超预算时翻译页应显示醒目警告（§15.3）。"""
    monkeypatch.setattr(PytranApp, "_estimate_tokens",
                        lambda self, only_uids=None: 10_000)
    gui._budget_max = 100
    gui._refresh_translate_info()
    assert not gui._widgets["budget_warn"].isHidden()
    assert "超出预算" in gui._widgets["budget_warn"].text()

    gui._budget_max = 999_999
    gui._refresh_translate_info()
    assert gui._widgets["budget_warn"].isHidden()


def test_translate_info_shows_model_and_estimate(gui: PytranApp, monkeypatch) -> None:
    """§9 L483 / C5：翻译页应展示生效参数与估算 token。"""
    monkeypatch.setattr(PytranApp, "_estimate_tokens",
                        lambda self, only_uids=None: 1234)
    gui._refresh_translate_info()
    text = gui._widgets["translate_info"].text()
    assert "模型：" in text
    assert "并发" in text
    assert "1,234" in text
    assert "待译" in text


# --------------------------------------------------------------------------- #
# C3/C4：worker 不得直接改 state，只能发事件
# --------------------------------------------------------------------------- #
def test_goto_step_event_updates_state_on_main_thread(gui: PytranApp) -> None:
    """C3/C4 回归：切页走事件，由主线程执行。"""
    gui.state.goto(2)
    gui._apply_event("goto_step", {"step": 3})
    assert gui.state.current_step == 3, "goto_step 事件应把状态切到审校页"
    assert gui._widgets["stack"].currentIndex() == 3


def test_goto_step_event_rejects_out_of_range(gui: PytranApp) -> None:
    """非法 step 不得把 stack 索引打到越界（Qt 会告警/崩溃）。"""
    gui.state.goto(2)
    gui._apply_event("goto_step", {"step": 99})
    assert gui.state.current_step == 2, "越界 step 应被忽略"


def test_translate_in_background_sends_goto_event_not_direct_write(
    gui: PytranApp, monkeypatch
) -> None:
    """worker 完成时必须发 goto_step 事件，而不是直接写 state.current_step。"""
    import threading

    events = []
    done = threading.Event()
    real_on_event = gui.on_event

    def spy(name, data):
        # 保留真实行为（入队），同时记录供断言
        real_on_event(name, data)
        events.append(name)
        if name == "translate_finished":
            done.set()

    monkeypatch.setattr(PytranApp, "_ensure_engine_ready", lambda self: True)

    class FakeEngine:
        def run(self, project, config, profile, on_event=None, only_uids=None):
            return project

    class FakeDeps:
        engine = FakeEngine()
        config = None

    gui.deps = FakeDeps()
    monkeypatch.setattr(gui, "_load_profile", lambda: type("P", (), {})())
    monkeypatch.setattr(gui, "on_event", spy)
    gui._translate_in_background()

    # worker 在后台线程：等它发完 finally 里的事件
    assert done.wait(timeout=5.0), "worker 应在 5 秒内完成"

    assert "goto_step" in events, "worker 完成应发 goto_step 事件（§9 红线）"
    assert "translate_finished" in events
    # §9 红线：worker 只发事件，不得直接写 UI 状态
    assert gui.state.current_step != 3, \
        "worker 不得直接写 state.current_step，必须由主线程消费 goto_step"
    # 主线程消费事件后才会真正切页
    gui._drain_events()
    assert gui.state.current_step == 3
    assert gui._widgets["stack"].currentIndex() == 3
