"""应用图标接入的冒烟验证（W6 交付物）。

不启动事件循环，只验证：
* ``PytranApp._icon_path()`` 在仓库内能命中真实文件；
* 该 ico 可被 Qt 加载，且 16/32/48/256 各档齐全；
* ``run()`` 结束后 QApplication 的 windowIcon 非空（即真的设上了）。

与其余 UI 测试同款前置：无显示环境用 offscreen 平台，且必须在
import Qt 之前设置（Qt 只在首次构造 QApplication 时读该变量）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="图标测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QIcon  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from pytran.models import Project  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402

REQUIRED_SIZES = (16, 32, 48, 256)


def test_icon_file_resolves_in_repo() -> None:
    """纯路径断言，不碰 Qt（图标是打包资源，路径契约独立可测）。"""
    path = PytranApp._icon_path()
    assert path.is_file(), f"图标缺失：{path}"
    assert path.stat().st_size > 1024, f"图标异常小，可能没写全：{path}"


def test_qt_can_load_all_icon_sizes(qt_app_qapp) -> None:
    # QIcon 必须在 QApplication 之后构造，否则 Qt 直接崩（非 Python 异常）。
    icon = QIcon(str(PytranApp._icon_path()))
    assert not icon.isNull(), "Qt 无法加载 pytran.ico"
    present = {s.width() for s in icon.availableSizes()}
    for want in REQUIRED_SIZES:
        assert want in present, f"ico 缺少 {want}x{want} 档，现有 {sorted(present)}"


def test_run_sets_window_icon(qt_app_qapp, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QDialog, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: None, raising=False)
    app = PytranApp(Project(), work_dir=tmp_path)
    app._log = lambda *a, **k: None
    app.run()
    try:
        assert not qt_app_qapp.windowIcon().isNull(), "run() 未设置应用图标"
    finally:
        if app._timer is not None:
            app._timer.stop()
        if app._window is not None:
            app._window.close()


@pytest.fixture(scope="module")
def qt_app_qapp():
    """模块级 QApplication：offscreen 下复用一个实例，避免反复构造。"""
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover - 无 Qt 平台插件时跳过
        pytest.skip(f"无法创建 QApplication：{exc}")
