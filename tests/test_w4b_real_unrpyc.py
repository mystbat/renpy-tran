"""W4b 真实上游 unrpyc 全链路验证（任务 W4b / ADR-0008 §8.5）。

证明目标（与任务拆分一致）：用**真实打包游戏**的 .rpyc，经由项目薄封装
``UnrenImpl.decompile()`` → ``vendor.unrpyc`` 子进程桥 → **真实上游**
``vendor/unrpyc_real``（CensoredUsername + F95Sam, MIT, commit 44febb0）解码出
真实 Ren'Py 源码，端到端跑通。

关键约束（见任务拆分 §0.2 / ADR-0008）：
- 真实上游在模块加载时 ``import renpy.object``，因此**不能在项目解释器内**
  导入，只能在持有真实 Ren'Py 引擎的子进程里运行 —— 这正是设计 §8.5 的
  SDK-bridge 形态。
- Ren'Py 的嵌入式 python 依赖其**原始安装路径**，复制后无法独立运行，因此本
  测试通过 ``decompile(..., python_exe=, sdk_root=)`` 显式传入真实 SDK 的
  解释器与引擎目录（真实打包游戏在其自身原始路径下即为此形态）。
- DSH 沙箱仅允许写入 workspace（``E:/Gameditor/py-tran``），且子进程无法写
  系统临时目录；故忠实游戏副本构建在仓库内 ``.w4b_real_game/``（已 gitignore）。

若本机未部署 Ren'Py 8.3.7 SDK，则优雅 skip（不阻塞 CI）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pytran.unren import UnrenImpl

# 真实 SDK 位置（任务环境提供）。缺失则跳过。
SDK_ROOT = Path(r"E:\Gameditor\renpy-8.3.7-sdk")
SDK_PYTHON = SDK_ROOT / "lib" / "py3-windows-x86_64" / "python.exe"

# 忠实副本构建在仓库内（gitignore 已忽略 .w4b_real_game/）。
REPLICA = Path(__file__).resolve().parents[1] / ".w4b_real_game"

# 经探针确认可在 8.3.7 (commit 44febb0) 上解码的真实 .rpyc（launcher 样例）。
REAL_RPYC = ["ability.rpyc", "androidstrings.rpyc", "archiver.rpyc"]
SOURCE_GAME = SDK_ROOT / "launcher" / "game"


def _sdk_available() -> bool:
    return (
        SDK_ROOT.is_dir()
        and SDK_PYTHON.is_file()
        and (SDK_ROOT / "renpy").is_dir()
        and all((SOURCE_GAME / n).is_file() for n in REAL_RPYC)
    )


@pytest.fixture(scope="session")
def real_game_replica() -> Path:
    """在仓库内构建一个忠实游戏副本（含真实引擎 + 真实 .rpyc）。

    副本布局满足 ``is_renpy_game``（game/ + renpy/）与 ``detect_game_python``
    （lib/py3-windows-x86_64/python.exe）；但副本内的 python 为占位（不可独立
    运行），实际解码经由 fixture 注入的真实 SDK 解释器完成。
    """
    if not _sdk_available():
        pytest.skip("未部署 Ren'Py 8.3.7 SDK，跳过真实解码验证")

    if REPLICA.exists():
        shutil.rmtree(REPLICA)
    (REPLICA / "game").mkdir(parents=True)
    (REPLICA / "renpy").mkdir(parents=True)
    (REPLICA / "lib" / "py3-windows-x86_64").mkdir(parents=True)

    for name in REAL_RPYC:
        shutil.copy(SOURCE_GAME / name, REPLICA / "game" / name)
    # 真实引擎目录（用于忠实布局 + detect_sdk_root 回退场景）。
    shutil.copytree(SDK_ROOT / "renpy", REPLICA / "renpy", dirs_exist_ok=True)
    # 占位解释器（真实解码不使用它，仅用于布局完整性）。
    shutil.copy(
        SDK_PYTHON, REPLICA / "lib" / "py3-windows-x86_64" / "python.exe"
    )
    return REPLICA


def test_w4b_real_unrpyc_end_to_end(real_game_replica: Path) -> None:
    """UnrenImpl.decompile() 经真实上游子进程桥解真实 .rpyc。

    断言：返回 == 真实 .rpyc 数量 个 .rpy（验证 §0 去重修复），且至少一份产物
    是真实 Ren'Py 源码（含 ``init`` 或 ``python`` 关键字），而非合成 fast-path
    的标记提取。
    """
    u = UnrenImpl()
    # 忠实布局可通过自动探测识别为游戏。
    assert u.is_renpy_game(real_game_replica) is True

    produced = u.decompile(
        real_game_replica,
        python_exe=SDK_PYTHON,
        sdk_root=SDK_ROOT,
    )
    # 每个真实 .rpyc 恰好解码一次（验证去重修复）。
    assert len(produced) == len(REAL_RPYC), produced

    real_source = 0
    for p in produced:
        p = Path(p)
        assert p.suffix == ".rpy", p
        text = p.read_text(encoding="utf-8", errors="replace")
        assert len(text) > 0
        if "init" in text or "python" in text:
            real_source += 1
    assert real_source >= 1, "未产出真实 Ren'Py 源码"


def test_w4b_real_unrpyc_produces_real_source(real_game_replica: Path) -> None:
    """产物为真实上游解码（非合成 marker 提取）：ability 含 ``init 1 python in``。"""
    u = UnrenImpl()
    produced = u.decompile(
        real_game_replica,
        python_exe=SDK_PYTHON,
        sdk_root=SDK_ROOT,
    )
    by_name = {Path(p).name: Path(p) for p in produced}
    ability = by_name.get("ability.rpy")
    assert ability is not None, list(by_name)
    text = ability.read_text(encoding="utf-8", errors="replace")
    assert "init 1 python in ability" in text, repr(text[:80])
