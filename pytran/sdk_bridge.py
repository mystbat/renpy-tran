"""Ren'Py SDK 集成（tl 骨架生成，设计 §8.5）。

实现 `interfaces.SdkBridge`：

* ``probe(sdk_dir, game_root)`` —— 校验 SDK 目录（含 renpy launcher），并从游戏
  ``lib/`` 子目录名推断引擎版本（``py2-*`` → "7.x"，``py3-*`` → "8.x"）。
* ``generate(sdk_dir, game_root, language, work_dir)`` —— 优先无头生成入口；
  否则用**桥接脚本**方案：临时写入 ``game/_pytran_gentl.rpy``（其 init python
  调用 ``renpy.translation.generation.generate_translations(<lang>)``），用 SDK
  解释器无界面启动游戏、执行完自动退出，随后删除临时脚本并记 journal（§15.2）；
  任一步失败回退内置近似骨架（unren._builtin_skeleton）。

无本机 SDK 时所有真调用被 skip，仅测试内置回退路径，保证可单测（设计 §15.5）。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import List, Optional

from .interfaces import SdkBridge, WriteJournal
from .journal import JsonWriteJournal

logger = logging.getLogger(__name__)

# 桥接脚本模板：官方生成路径（§8.5 第 2 点）
_BRIDGE_TEMPLATE = """\
# 由 pytran 自动生成的临时桥接脚本（§8.5），执行后删除。
init 1 python:
    import renpy.translation.generation as _gtg
    try:
        _gtg.generate_translations({language!r})
    except TypeError:
        # 旧版 API 不接受关键字参数
        _gtg.generate_translations({language!r})
    renpy.quit()
"""

_BRIDGE_FILE = "_pytran_gentl.rpy"


class SdkBridgeImpl(SdkBridge):
    """`SdkBridge` 的默认实现。"""

    def probe(self, sdk_dir: Path, game_root: Path) -> Optional[str]:
        sdk_dir = Path(sdk_dir)
        game_root = Path(game_root)
        if not sdk_dir.is_dir():
            return None
        markers = ("renpy.exe", "renpy.sh", "renpy.py")
        if not any((sdk_dir / m).exists() for m in markers):
            return None
        # 从游戏 lib/ 子目录名推断引擎版本（§8.5）
        lib = game_root / "lib"
        if lib.is_dir():
            for sub in lib.iterdir():
                if not sub.is_dir():
                    continue
                name = sub.name
                if name.startswith("py3-"):
                    return "8.x"
                if name.startswith("py2-"):
                    return "7.x"
        # 无法确定时按 SDK 自带解释器目录再判断一次
        sdk_lib = sdk_dir / "lib"
        if sdk_lib.is_dir():
            for sub in sdk_lib.iterdir():
                if not sub.is_dir():
                    continue
                if sub.name.startswith("py3"):
                    return "8.x"
                if sub.name.startswith("py2"):
                    return "7.x"
        return "8.x"  # 默认按新版处理

    def generate(
        self,
        sdk_dir: Path,
        game_root: Path,
        language: str,
        work_dir: Path,
    ) -> List[Path]:
        sdk_dir = Path(sdk_dir)
        game_root = Path(game_root)
        work_dir = Path(work_dir)

        version = self.probe(sdk_dir, game_root)
        if version is None:
            logger.warning("SDK 目录无效，回退内置骨架")
            return _fallback(game_root, language)

        journal = JsonWriteJournal(work_dir)
        bridge = game_root / "game" / _BRIDGE_FILE
        journal_id = journal.begin(f"sdk_generate:{game_root}")
        launcher = _launcher_for(sdk_dir)
        try:
            bridge_text = _BRIDGE_TEMPLATE.format(language=language)
            backup = None
            if bridge.exists():
                backup = work_dir / "bak" / "game" / _BRIDGE_FILE
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(bridge.read_text(encoding="utf-8"), encoding="utf-8")
                journal.record(journal_id, "temp_bridge", backup, bridge, backup)
            else:
                journal.record(journal_id, "temp_bridge", work_dir, bridge, None)
            bridge.write_text(bridge_text, encoding="utf-8")

            # 用 SDK 解释器无界面启动游戏执行桥接脚本（§8.5 第 2 点）
            cmd = [str(launcher), str(game_root)]
            logger.info("调用 SDK 生成骨架：%s", " ".join(cmd))
            try:
                subprocess.run(cmd, check=True, timeout=600, capture_output=True)
            except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("SDK 生成调用失败，回退内置骨架：%s", exc)
                _cleanup_bridge(bridge, journal, journal_id, backup)
                return _fallback(game_root, language)

            # 执行成功：删除临时桥接脚本并记 journal（§15.2）
            if bridge.exists():
                bridge.unlink()
            journal.record(journal_id, "remove_bridge", work_dir, bridge, backup)
            journal.commit(journal_id)

            files = _collect_tl(game_root, language)
            if files:
                logger.info("SDK 生成 %d 个 tl 文件（版本=%s）", len(files), version)
                return files
            logger.warning("SDK 未产出 tl 文件，回退内置骨架")
            return _fallback(game_root, language)
        except Exception:
            try:
                journal.rollback(journal_id)
            except Exception as rb:  # pragma: no cover - defensive
                logger.error("SDK 生成回滚失败：%s", rb)
            raise


# -- 模块级辅助 -------------------------------------------------------------


def _launcher_for(sdk_dir: Path) -> Optional[Path]:
    for name in ("renpy.exe", "renpy.sh", "renpy.py"):
        p = sdk_dir / name
        if p.exists():
            return p
    return None


def _cleanup_bridge(
    bridge: Path, journal: WriteJournal, journal_id: str, backup: Optional[Path]
) -> None:
    if bridge.exists():
        try:
            bridge.unlink()
        except OSError:  # pragma: no cover - defensive
            pass
    try:
        journal.rollback(journal_id)
    except Exception:  # pragma: no cover - defensive
        pass


def _collect_tl(game_root: Path, language: str) -> List[Path]:
    tl_dir = game_root / "game" / "tl" / language
    if not tl_dir.is_dir():
        return []
    return sorted(tl_dir.rglob("*.rpy"))


def _fallback(game_root: Path, language: str) -> List[Path]:
    from .unren import _builtin_skeleton

    return _builtin_skeleton(game_root, language)


def make_sdk_bridge() -> SdkBridgeImpl:
    """工厂：返回默认 SdkBridge 实现。"""
    return SdkBridgeImpl()


__all__ = ["SdkBridgeImpl", "make_sdk_bridge"]
