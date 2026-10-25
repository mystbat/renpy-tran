"""GUI 视图层无头冒烟（W5 交付物，设计 §9）。

不启动真实 Tk（``run()`` 需显示，已标 pragma no cover）；仅验证：
* ``pytran.ui`` 可在无头环境 import（tkinter 延迟导入）；
* ``PytranApp`` 构造 + 事件桥（后台 put / 主线程 drain）不依赖 Tk 对象；
* 无 root 时 ``_drain_events`` / ``_apply_event`` 安全空转。
"""

from __future__ import annotations

from pytran.models import Project
from pytran.ui import PytranApp


def test_ui_import_no_tkinter_side_effect() -> None:
    import pytran.ui  # 已在导入期验证；这里再确认模块可用
    assert hasattr(pytran.ui, "PytranApp")


def test_pytran_app_construction_and_event_bridge(tmp_path) -> None:
    proj = Project(segments=[])
    # 隔离工作目录：绝不能用 "."——真实应用的 work_dir 就是仓库根，
    # 那里放着用户自己的 project.json / profile.json（见 conftest 守卫）。
    app = PytranApp(proj, work_dir=str(tmp_path))
    # 后台线程只发事件：入队
    app.on_event("log", {"message": "hello"})
    app.on_event("progress", {})
    assert app._events.qsize() == 2
    # 无 root 时 drain 安全空转（不抛、不阻塞）
    app._drain_events()
    assert app._events.empty()
