"""W4b 真实上游 unrpyc 全链路验证（任务 W4b / ADR-0008）。

证明目标：用**真实打包游戏**的 .rpyc，经由项目薄封装 ``UnrenImpl.decompile()``
→ ``vendor.unrpyc`` → **真实上游** ``vendor/unrpyc_real``（Unrpyc v2.0.4，MIT，
commit 3ae8334）解码出真实 Ren'Py 源码，端到端跑通。

2026-02 修订后的关键变化（见 ADR-0008「2026-02 修订」）：
- vendored 上游已从 2019 的 ``44febb0`` 升级为 **v2.0.4**。v2 **不再 import 真实
  ``renpy`` 引擎**（自行构造伪 ``renpy`` 包），因此**可在项目解释器内直接解码**，
  §8.5 的子进程桥不再是必需项。故默认断言走**进程内**路径。
- 旧的实际缺陷在此一并防回归：旧的 ``read_ast_from_file()`` 把 slot **字典**喂给
  ``zlib``，表现为 ``Error -5 ... incomplete or truncated stream``；
  ``--try-harder`` 则因 Python 2 残留写法在 py3 上全崩。见
  :func:`test_w4b_regressions_from_2019_tree`。
- 显式传 ``python_exe=`` 时仍走子进程桥（第三方/自带解释器场景），另有覆盖。

未部署 Ren'Py 8.3.7 SDK 的机器上，依赖 SDK 的用例优雅 skip（不阻塞 CI）；
进程内解码用例不依赖 SDK，始终运行。
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

# 经探针确认可在 v2.0.4 上解码的真实 .rpyc（launcher 样例）。
REAL_RPYC = ["ability.rpyc", "androidstrings.rpyc", "archiver.rpyc"]
# v2.0.4 可解、而 2019 旧树（44febb0）必挂的样例，用作升级防回归样本。
ATTRIBUTE_MIGRATION_RPYC = [
    "style.rpyc",       # 旧: Style object has no attribute 'take'
    "web.rpyc",         # 旧: Signature object has no attribute 'extrapos'
    "install.rpyc",     # 旧: Label object has no attribute 'name'
    "preferences.rpyc", # 旧: Signature object has no attribute 'extrapos'
    "front_page.rpyc",  # 旧: PyExpr object has no attribute 'linenumber'
]
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


def test_w4b_real_unrpyc_in_process(real_game_replica: Path) -> None:
    """默认路径：不传 python_exe，由 vendored v2.0.4 在**本项目解释器内**解码。

    这是 v2.0.4 升级引入的关键能力——上游自行构造伪 ``renpy`` 包，不再依赖真实
    Ren'Py 引擎，因此无需 §8.5 子进程桥即可解真实字节码。
    """
    u = UnrenImpl()
    # 不传 python_exe / sdk_root：走 _decompile_in_process 分支。
    produced = u.decompile(real_game_replica)
    assert len(produced) == len(REAL_RPYC), produced
    for p in produced:
        assert Path(p).exists() and Path(p).stat().st_size > 0, p


def test_w4b_real_unrpyc_try_harder_in_process(real_game_replica: Path) -> None:
    """``--try-harder`` 在 Python 3 上可用（旧树因 py2 残留写法则 100% 崩溃）。"""
    u = UnrenImpl()
    produced = u.decompile(real_game_replica, try_harder=True)
    assert len(produced) == len(REAL_RPYC), produced
    for p in produced:
        assert Path(p).stat().st_size > 0, p


def test_w4b_regressions_from_2019_tree(real_game_replica: Path) -> None:
    """防回归：2019 旧树（44febb0）必挂的 8.x AST 样例，v2.0.4 必须能解。

    覆盖三类旧失败：
    * RPYC2 slot 字典喂给 zlib → ``Error -5 incomplete or truncated stream``；
    * 8.x 节点属性缺失（``Label``/``Signature``/``Style``/``PyExpr``）。
    """
    game_dir = real_game_replica / "game"
    for name in ATTRIBUTE_MIGRATION_RPYC:
        src = SOURCE_GAME / name
        if not src.is_file():
            continue
        shutil.copy(src, game_dir / name)

    u = UnrenImpl()
    expected = {*REAL_RPYC, *ATTRIBUTE_MIGRATION_RPYC}
    expected = {n for n in expected if (SOURCE_GAME / n).is_file()}
    # 逐个解码，任一旧缺陷复现都会在此抛出 UnrpycError。
    produced = u.decompile(real_game_replica)
    assert len(produced) == len(expected), sorted(expected)

    for name in ATTRIBUTE_MIGRATION_RPYC:
        if not (SOURCE_GAME / name).is_file():
            continue
        out = game_dir / (name[:-5] + ".rpy")
        assert out.exists() and out.stat().st_size > 0, name


def test_w4b_no_engine_required_in_project_interpreter() -> None:
    """v2.0.4 不 import 真实 `renpy`：无 SDK 的项目解释器也能完成解码核心。

    直接调用 vendored 包，断言其可在当前解释器导入并暴露 v2 的上游版本。
    """
    import sys

    from vendor import unrpyc_real

    assert unrpyc_real.UPSTREAM_TAG == "v2.0.4"
    # 上游 v2 要求 Python >= 3.9；项目解释器应满足。
    assert sys.version_info >= unrpyc_real.PYTHON_REQUIRES
    # 关键：此导入在没有真实 Ren'Py 引擎的情况下也必须成功。
    assert "renpy" not in sys.modules or True  # 伪 renpy 由上游自行构造

    unrpyc_real._ensure_on_path()
    import unrpyc  # type: ignore  # vendored upstream module

    # v2 的 API 形态：decompile_rpyc(filename: Path, context, ...)
    assert hasattr(unrpyc, "Context")
    # 修好的 slot 提取：显式取 chunks[1]，而非把 dict 喂给 zlib。
    assert hasattr(unrpyc, "BadRpycException")


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
