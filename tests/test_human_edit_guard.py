"""人工修改保护（BUGFIX human-edit-guard, 2026-10-26）回归测试。

用户实测：uid=6274 在审校界面手工改过译文并保存，下一轮「重试失败项」
把手工改动**无条件覆盖**回模型输出——failed 行重新进队列时没有任何
"有人改过"的记忆。

本文件锁定修复后的行为：
* 审校界面保存（_on_save / _stash_if_dirty / _on_accept）→ uid 进入
  ``_human_edited_uids``；
* 「重试失败项」遇到人工修改过的 FAILED 行 → 弹三选（跳过/覆盖/取消）；
  选跳过 → 这些行不进队列。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法创建 QApplication：{exc}")


@pytest.fixture(autouse=True)
def _no_event_loop(monkeypatch):
    """run() 末尾的 app.exec() 会阻塞测试进程——与 test_ui_writeback_fuse
    同样的处理：整体 patch 掉对话框与事件循环。"""
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No,
                        raising=False)


def _failed_seg(uid: int) -> Segment:
    return Segment(uid=uid, file="a.rpy", locator=f"# game/a.rpy:{uid}",
                   feature="f", kind="dialogue", comment="", role="",
                   source=f"s{uid}", target=f"t{uid}",
                   status=SegmentStatus.FAILED)


def _make_app(qt_app, tmp_path, segments):
    app = PytranApp(Project(segments=segments, game_root=""),
                    work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    return app


def test_save_marks_human_edited(qt_app, tmp_path) -> None:
    """保存译文 → uid 进入 _human_edited_uids。"""
    app = _make_app(qt_app, tmp_path, [_failed_seg(1)])
    # 选到 uid=1 并保存
    app._on_save()  # 当前游标即 uid=1（dirty 未置位不覆盖 target，但旧实现 _on_save 无条件写）
    # 无编辑时也应通过 current_review 定位游标
    assert app.state._review_index == 0
    # 模拟真实编辑流：置脏 → 保存
    app._review_dirty = True
    app.state._review_index = 0
    app._on_save()
    assert 1 in app._human_edited_uids


def test_stash_marks_human_edited(qt_app, tmp_path) -> None:
    """切换条目时暂存未保存修改 → 同样记入人工修改集合。"""
    app = _make_app(qt_app, tmp_path, [_failed_seg(2)])
    app.state._review_index = 0
    app._review_dirty = True
    app._stash_if_dirty()
    assert 2 in app._human_edited_uids


def test_retry_skips_human_edited(qt_app, tmp_path, monkeypatch) -> None:
    """重试失败项：人工修改行被跳过，其余照常进队列。"""
    app = _make_app(qt_app, tmp_path,
                    [_failed_seg(3), _failed_seg(4)])

    started: dict = {}

    def _fake_translate(only_uids=None, *, auto_goto=True):
        started["only_uids"] = set(only_uids or set())

    app._translate_in_background = _fake_translate
    app._confirm_budget = lambda only_uids=None: True
    app._ensure_engine_ready = lambda: True   # 未配置 Key 的测试环境跳过引擎检查

    # uid=3 被人工修改过
    app._human_edited_uids.add(3)

    # 弹框选「跳过」（第一个 addButton，AcceptRole）
    from PyQt6.QtWidgets import QMessageBox as _QMB

    def _skip_exec(self):
        # 把 clickedButton 指到第一个按钮（跳过）
        buttons = self.buttons()
        self.clickedButton = lambda: buttons[0]
        return 0

    monkeypatch.setattr(_QMB, "exec", _skip_exec, raising=False)

    app._retry_failed()

    assert 3 not in started["only_uids"], "人工修改行必须被跳过"
    assert 4 in started["only_uids"], "未修改行照常重译"
