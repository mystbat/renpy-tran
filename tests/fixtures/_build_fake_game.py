#!/usr/bin/env python3
"""生成 W4 测试用的合成小型游戏目录树（设计 §15.2 / §8 fixtures 需求）。

生成物位于 ``tests/fixtures/fake_game/``，包含：

* ``renpy/``           —— 空目录（is_renpy_game 判定必需）
* ``lib/py3-windows-x86_64/python.exe`` —— 游戏自带解释器占位（detect_game_python 探测）
* ``game/script.rpy``  + ``game/script.rpyc`` —— 手写 rpyc，内部用 ``# src_begin`` /
  ``# src_end`` 标记嵌入原始 rpy 源码（fake_unrpyc 反编译可恢复）
* ``game/packed.rpa``  —— 由 ``vendor.rpatool.create`` 打包一个极小 rpy

这些产物随仓库提交（.gitignore 已 whitelist ``tests/fixtures/**``），同时保留本脚本
以便随时重建。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent  # tests/fixtures -> repo root
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FAKE_GAME = HERE / "fake_game"

SCRIPT_RPY = """\
# 合成游戏脚本（用于反编译/写回测试）
translate chinese start_1:
    "Hello, world."
    "This is a fake Ren'Py game for pytran testing."
"""

PACKED_RPY = """\
# 被打包进 RPA 的文件
translate chinese packed_1:
    "Content inside an RPA archive."
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build() -> Path:
    # 清理旧产物后重建，保证幂等
    if FAKE_GAME.exists():
        import shutil

        shutil.rmtree(FAKE_GAME)

    # renpy/ 空目录 + gitkeep（git 不跟踪空目录）
    gitkeep = FAKE_GAME / "renpy" / ".gitkeep"
    gitkeep.parent.mkdir(parents=True, exist_ok=True)
    gitkeep.write_text("", encoding="utf-8")

    # 游戏自带解释器占位（非真实 exe，仅用于探测）
    _write(FAKE_GAME / "lib" / "py3-windows-x86_64" / "python.exe", "")

    # 主脚本 + 手写 rpyc（内嵌原始源码）
    game = FAKE_GAME / "game"
    _write(game / "script.rpy", SCRIPT_RPY)
    rpyc_content = (
        "# 手写合成 rpyc（fake_unrpyc 通过标记恢复源码）\n"
        "# src_begin\n"
        + SCRIPT_RPY
        + "# src_end\n"
    )
    _write(game / "script.rpyc", rpyc_content)

    # 打包一个极小 rpy 进 RPA（源文件置于 fake_game 根外，避免扫描冲突）
    src = FAKE_GAME / "_rpa_src" / "packed.rpy"
    _write(src, PACKED_RPY)
    from vendor import rpatool

    rpatool.create([src], game / "packed.rpa", prefix="")

    print(f"built fake game at {FAKE_GAME}")
    return FAKE_GAME


if __name__ == "__main__":
    build()
