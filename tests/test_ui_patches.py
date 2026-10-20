"""辅助补丁工具区的回归测试（A4）。

设计文档 §9 布局图要求工具区含
``[辅助补丁□控制台□快存□跳过□回滚] [移除补丁]``，§8.1/§8.4 规定：
四个补丁**修改游戏行为**、与翻译无关，仅调试便利；**默认不启用**、
独立勾选区、随时可一键移除、写入与移除都记日志。

``unren.py`` 的 ``apply_patch`` / ``remove_patches`` 早已实现（W4），
但 UI 侧一直没有入口（W4 交付说明第 75 行「UI 勾选交由 W5」，W5 未交付）。

本模块锁定 UI 侧行为：控件存在、默认不勾、按 game_root 置灰、
幂等（已注入的拒绝覆盖）、移除后状态复位。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="辅助补丁测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QDialog, QGroupBox, QMessageBox, QPushButton,
)

from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


def _make_game(tmp_path: Path) -> Path:
    """构造最小 Ren'Py 游戏目录树（game/ + renpy/ + lib/py3-*）。"""
    root = tmp_path / "game_root"
    (root / "game").mkdir(parents=True)
    (root / "renpy").mkdir(parents=True)
    (root / "lib" / "py3-windows-x86_64").mkdir(parents=True)
    return root


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
    app = PytranApp(
        Project(segments=[Segment(
            uid=1, file="a.rpy", locator="l", feature="f", kind="dialogue",
            comment="", role="", source="x", status=SegmentStatus.PENDING)],
            game_root=""),
        work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    yield app
    if app._timer is not None:
        app._timer.stop()
    if app._window is not None:
        app._window.close()


def _patch_group(gui: PytranApp) -> QGroupBox:
    group = gui._widgets.get("patch_group")
    assert group is not None, "选择目录页应有辅助补丁工具区（A4）"
    return group


# --------------------------------------------------------------------------- #
# 控件存在性与默认状态
# --------------------------------------------------------------------------- #
def test_patch_tool_area_exists_with_four_checkboxes(gui: PytranApp) -> None:
    """A4：§9 工具区必须有 4 个补丁勾选框 + 注入/移除按钮。"""
    _patch_group(gui)
    assert set(gui._patch_boxes) == {"console", "quick", "skip", "rollback"}, \
        "四个补丁（§8.1 对照表）都必须有勾选框"
    for key, cb in gui._patch_boxes.items():
        assert not cb.isChecked(), f"{key} 默认不应勾选（§8.4：默认不启用）"
    for key in ("btn_apply_patch", "btn_remove_patch"):
        assert key in gui._widgets, f"缺少 {key} 按钮"


def test_patch_area_disabled_without_game_root(gui: PytranApp) -> None:
    """§8.2/§8.4：纯文件模式（无 game_root）时补丁区置灰。"""
    gui.project.game_root = ""
    gui._refresh_patch_ui()
    assert not _patch_group(gui).isEnabled()
    assert "仅游戏目录模式" in gui._widgets["patch_status"].text()


def test_patch_area_enabled_with_game_root(gui: PytranApp, tmp_path: Path) -> None:
    gui.project.game_root = str(_make_game(tmp_path))
    gui._refresh_patch_ui()
    assert _patch_group(gui).isEnabled()
    assert "尚未注入" in gui._widgets["patch_status"].text()


# --------------------------------------------------------------------------- #
# 注入 / 移除
# --------------------------------------------------------------------------- #
def test_apply_patches_writes_only_checked(gui: PytranApp, tmp_path: Path) -> None:
    """只写勾中的补丁，未勾选的不碰（§8.4 独立勾选）。"""
    root = _make_game(tmp_path)
    gui.project.game_root = str(root)
    gui._patch_boxes["skip"].setChecked(True)   # 只勾 skip
    gui._patch_boxes["console"].setChecked(False)

    gui._on_apply_patches()

    game = root / "game"
    assert (game / "unren-skip.rpy").exists(), "勾选的 skip 应被写入"
    assert not (game / "unren-console.rpy").exists(), "未勾选的 console 不应写入"
    # §8.4：统一 unren-*.rpy 命名
    assert (game / "unren-skip.rpy").read_text(encoding="utf-8").startswith("init 999 python:")


def test_apply_patches_idempotent_refuses_overwrite(gui: PytranApp, tmp_path: Path) -> None:
    """§8.4 幂等：已存在的补丁拒绝覆盖（apply_patch 抛 GameError）。"""
    root = _make_game(tmp_path)
    gui.project.game_root = str(root)
    gui._patch_boxes["console"].setChecked(True)

    gui._on_apply_patches()
    first = (root / "game" / "unren-console.rpy").read_text(encoding="utf-8")

    # 用户改动文件后再注入 → 应被拒绝，内容不被覆盖
    (root / "game" / "unren-console.rpy").write_text("# 用户改动", encoding="utf-8")
    gui._on_apply_patches()
    assert (root / "game" / "unren-console.rpy").read_text(encoding="utf-8") == "# 用户改动", \
        "已存在的补丁必须拒绝覆盖（§8.4 幂等）"


def test_remove_patches_clears_all(gui: PytranApp, tmp_path: Path) -> None:
    """「移除补丁」一键卸载全部 unren-*.rpy。"""
    root = _make_game(tmp_path)
    gui.project.game_root = str(root)
    for key in ("console", "quick", "skip", "rollback"):
        gui._patch_boxes[key].setChecked(True)
    gui._on_apply_patches()
    assert len(list((root / "game").glob("unren-*.rpy"))) == 4

    gui._on_remove_patches()
    assert list((root / "game").glob("unren-*.rpy")) == [], "移除后不得残留 unren-*.rpy"
    # 移除后状态标签复位
    gui._refresh_patch_ui()
    assert "尚未注入" in gui._widgets["patch_status"].text()


def test_remove_patches_idempotent(gui: PytranApp, tmp_path: Path) -> None:
    """没有补丁时再次移除不得报错（幂等）。"""
    root = _make_game(tmp_path)
    gui.project.game_root = str(root)
    gui._on_remove_patches()   # 不得抛
    gui._on_remove_patches()   # 再点一次也不得抛


def test_apply_patches_without_selection_warns(gui: PytranApp, tmp_path: Path) -> None:
    """未勾选任何补丁时不得写入文件。"""
    root = _make_game(tmp_path)
    gui.project.game_root = str(root)
    for cb in gui._patch_boxes.values():
        cb.setChecked(False)
    gui._on_apply_patches()
    assert list((root / "game").glob("unren-*.rpy")) == []


def test_apply_patches_without_game_root_warns(gui: PytranApp) -> None:
    """纯文件模式下注入补丁应给出可读提示，不得写文件或崩溃。"""
    gui.project.game_root = ""
    gui._on_apply_patches()   # 不得抛
    gui._on_remove_patches()  # 不得抛


def test_injected_patches_shown_as_checked(gui: PytranApp, tmp_path: Path) -> None:
    """刷新时已注入的补丁应显示为勾选状态（用户可见当前注入了什么）。"""
    root = _make_game(tmp_path)
    gui.project.game_root = str(root)
    gui._patch_boxes["quick"].setChecked(True)
    gui._on_apply_patches()

    # 模拟一次界面刷新（如换目录后）：已注入的应被勾上
    for cb in gui._patch_boxes.values():
        cb.setChecked(False)
    gui._refresh_patch_ui()
    assert gui._patch_boxes["quick"].isChecked()
    assert not gui._patch_boxes["skip"].isChecked()
    assert "已注入" in gui._widgets["patch_status"].text()
