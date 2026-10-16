"""游戏解包 / 反编译 / 补丁注入 / 写回游戏目录（整合 UnRen，设计 §8）。

实现 `interfaces.Unren`：

* ``is_renpy_game`` / ``detect_game_python`` —— 结构检测与游戏自带解释器探测（§8.2）。
* ``extract_rpa`` —— 内置 ``vendor.rpatool`` 解包（§8.1 ①）。
* ``decompile`` —— 内置 ``vendor.unrpyc`` 反编译（§8.1 ②，``--try-harder`` 透传）。
* ``generate_skeleton`` —— SDK 优先、内置近似骨架兜底（§8.3 / §8.5）。
* ``apply_patch`` / ``remove_patches`` —— 四个辅助补丁模板（控制台/快存/跳过/回滚），
  统一命名 ``unren-*.rpy``，幂等可卸载（§8.1 ③④⑤⑥、§8.4）。
* ``write_back`` —— 事务化写回游戏目录，写前备份、记 journal、失败回滚（§15.2）。

所有写入统一走 ``bak/game/`` 备份 + ``logs/journal-*.json`` 审计；API Key / 配置内容
永不打印（§11）。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config
from .interfaces import GameError, Project, Unren, WriteJournal
from .journal import JsonWriteJournal
from .models import SegmentStatus
from .parser import RenpyParser

logger = logging.getLogger(__name__)

# 辅助补丁默认文件名前缀（§8.4：统一 unren-*.rpy）
_PATCH_PREFIX = "unren-"

# 四个补丁模板（等价 UnRen 的 init 999 python 块，§8.1）。热键以 {KEY_*} 占位，
# 由 apply_patch 的 hotkeys 参数替换，缺省沿用原版写死的键位。
_HOTKEY_DEFAULTS = {
    "KEY_QUICKSAVE": "K_F5",
    "KEY_QUICKLOAD": "K_F9",
    "KEY_SKIP": "K_TAB",
    "KEY_ROLLBACK": "K_PAGEUP",
}

PATCH_CONSOLE = """init 999 python:
    # UnRen: 开发者控制台（修改游戏行为，仅调试用）
    config.developer = True
    config.console = True
"""

PATCH_QUICK = """init 999 python:
    # UnRen: 快速存读档（修改游戏行为，仅调试用）
    config.quick_save_key = {KEY_QUICKSAVE}
    config.quick_load_key = {KEY_QUICKLOAD}
"""

PATCH_SKIP = """init 999 python:
    # UnRen: 强制跳过未读文本（修改游戏行为，仅调试用）
    config.skip_unseen = True
    config.allow_skipping = True
    config.fast_skipping = True
    config.skip_key = {KEY_SKIP}
"""

PATCH_ROLLBACK = """init 999 python:
    # UnRen: 强制回滚（修改游戏行为，仅调试用）
    config.rollback_enabled = True
    config.rollback_limit = 256
    config.block_rollback = False
    config.rollback_key = {KEY_ROLLBACK}
"""

# patch 名称 -> 模板
_PATCH_TEMPLATES = {
    "console": PATCH_CONSOLE,
    "quick": PATCH_QUICK,
    "skip": PATCH_SKIP,
    "rollback": PATCH_ROLLBACK,
}

# detect_game_python 的探测顺序（§8.2）
_PYTHON_DIR_PATTERNS = (
    "lib/py3-windows-*",
    "lib/windows-*",
    "lib/py2-windows-*",
    "lib/windows-i686",
)


class UnrenImpl(Unren):
    """`Unren` 的默认实现。"""

    # -- 结构检测 -----------------------------------------------------------

    def is_renpy_game(self, root: Path) -> bool:
        root = Path(root)
        return (root / "game").is_dir() and (root / "renpy").is_dir()

    def detect_game_python(self, root: Path) -> Optional[Path]:
        root = Path(root)
        lib = root / "lib"
        if not lib.is_dir():
            return None
        candidates: List[Path] = []
        for pattern in _PYTHON_DIR_PATTERNS:
            # 仅匹配 lib 的直接子目录
            prefix = pattern.split("*", 1)[0]  # e.g. "lib/py3-windows-"
            for sub in sorted(lib.iterdir()):
                if not sub.is_dir():
                    continue
                if pattern.endswith("*"):
                    if sub.name.startswith(prefix.split("/", 1)[1]):
                        candidates.append(sub)
                else:
                    if sub.name == pattern.split("/", 1)[1]:
                        candidates.append(sub)
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            exe = self._python_exe_in(cand)
            if exe is not None:
                return exe
        return None

    @staticmethod
    def _python_exe_in(d: Path) -> Optional[Path]:
        if os.name == "nt":
            for name in ("python.exe", "pythonw.exe"):
                p = d / name
                if p.is_file():
                    return p
        else:
            for name in ("python", "python3"):
                p = d / name
                if p.is_file():
                    return p
        return None

    def detect_sdk_root(self, root: Path) -> Optional[Path]:
        """Locate the Ren'Py engine dir (the dir containing ``renpy/``).

        A real packaged game carries its own ``renpy/`` at the game root, so the
        game root itself is the SDK root. This is also the directory that must be
        on ``sys.path`` for the upstream unrpyc to ``import renpy``. Returns the
        directory containing ``renpy/``, or ``None`` if not found. (§8.5)
        """
        root = Path(root)
        if (root / "renpy").is_dir():
            return root
        return None

    # -- 解包 / 反编译 -------------------------------------------------------

    def extract_rpa(
        self,
        game_root: Path,
        remove_rpa: bool = False,
        progress: "Callable[[str, int, int], None] | None" = None,
    ) -> List[Path]:
        game_root = Path(game_root)
        if not self.is_renpy_game(game_root):
            raise GameError(f"不是 Ren'Py 游戏目录：{game_root}")
        from vendor import rpatool

        extracted: List[Path] = []
        rpa_files = sorted(game_root.glob("game/**/*.rpa")) + sorted(
            game_root.glob("game/*.rpa")
        )
        seen_rpa = set()
        for rpa in rpa_files:
            if rpa in seen_rpa:
                continue
            seen_rpa.add(rpa)
            logger.info("解包 RPA: %s", rpa.name)
            cb = None
            if progress is not None:
                cb = (lambda rpa_name: (
                    lambda name, done, total: progress(
                        f"{rpa_name}: {name}", done, total)
                ))(rpa.name)
            out = rpatool.extract_archive(rpa, game_root / "game", progress=cb)
            extracted.extend(out)
            if remove_rpa:
                try:
                    rpa.unlink()
                except OSError as exc:  # pragma: no cover - defensive
                    logger.warning("无法删除 RPA %s: %s", rpa, exc)
        return extracted

    def decompile(
        self,
        game_root: Path,
        try_harder: bool = False,
        python_exe: Optional[Path] = None,
        sdk_root: Optional[Path] = None,
    ) -> List[Path]:
        game_root = Path(game_root)
        if not self.is_renpy_game(game_root):
            raise GameError(f"不是 Ren'Py 游戏目录：{game_root}")
        from vendor import unrpyc

        # `game/**/*.rpyc` already matches `game/*.rpyc`; dedupe so each file
        # is decoded exactly once.
        rpyc_files = sorted({*game_root.glob("game/**/*.rpyc")})
        if not rpyc_files:
            return []
        # Since the v2.0.4 upgrade the vendored upstream **no longer imports a
        # real Ren'Py engine** (it constructs a fake `renpy` package itself), so
        # it decodes in this interpreter. We therefore no longer auto-detect the
        # game's bundled python: doing so forced every real game down the
        # subprocess path even though none was needed, and games lacking a
        # usable bundled interpreter failed outright.
        # The subprocess path is now opt-in via an explicit python_exe=.
        logger.info(
            "反编译 %d 个 rpyc（try_harder=%s, python=%s, sdk_root=%s）",
            len(rpyc_files),
            try_harder,
            python_exe or "进程内（ vendored unrpyc v2，无需外部解释器）",
            sdk_root,
        )
        return unrpyc.decompile_files(
            rpyc_files,
            out_dir=None,
            try_harder=try_harder,
            python_exe=python_exe,
            sdk_root=sdk_root,
        )

    # -- 骨架生成 -----------------------------------------------------------

    def generate_skeleton(
        self, game_root: Path, language: str, config: Config
    ) -> List[Path]:
        game_root = Path(game_root)
        if not self.is_renpy_game(game_root):
            raise GameError(f"不是 Ren'Py 游戏目录：{game_root}")
        # SDK 优先（§8.5），无配置或失败回退内置近似骨架
        sdk = config.sdk_dir() if config is not None else None
        if sdk is not None:
            try:
                from .sdk_bridge import SdkBridge

                bridge = SdkBridge()
                if bridge.probe(sdk, game_root) is not None:
                    files = bridge.generate(
                        sdk, game_root, language, work_dir=game_root
                    )
                    if files:
                        return files
            except Exception as exc:  # pragma: no cover - 依赖本机 SDK
                logger.warning("SDK 骨架生成失败，回退内置骨架：%s", exc)
        return _builtin_skeleton(game_root, language)

    # -- 辅助补丁 -----------------------------------------------------------

    def apply_patch(
        self, game_root: Path, patch: str, hotkeys: dict[str, str] = {}
    ) -> Path:
        game_root = Path(game_root)
        if not self.is_renpy_game(game_root):
            raise GameError(f"不是 Ren'Py 游戏目录：{game_root}")
        template = _PATCH_TEMPLATES.get(patch)
        if template is None:
            raise GameError(f"未知补丁类型：{patch!r}（可选：{sorted(_PATCH_TEMPLATES)}）")
        game_dir = game_root / "game"
        game_dir.mkdir(parents=True, exist_ok=True)
        dst = game_dir / f"{_PATCH_PREFIX}{patch}.rpy"
        if dst.exists():
            # §8.4：已存在则拒绝覆盖（幂等 + 可卸载）
            raise GameError(f"补丁已存在，拒绝覆盖：{dst}（先 remove_patches）")
        text = _render_patch(template, hotkeys or {})
        dst.write_text(text, encoding="utf-8")
        logger.info("注入辅助补丁 %s（热键=%s）", patch, hotkeys or _HOTKEY_DEFAULTS)
        return dst

    def remove_patches(self, game_root: Path) -> List[Path]:
        game_root = Path(game_root)
        game_dir = game_root / "game"
        if not game_dir.is_dir():
            return []
        removed: List[Path] = []
        for p in sorted(game_dir.glob(f"{_PATCH_PREFIX}*.rpy")):
            try:
                p.unlink()
                removed.append(p)
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning("无法删除补丁 %s: %s", p, exc)
        return removed

    # -- 事务化写回 ---------------------------------------------------------

    def write_back(
        self, project: Project, work_dir: Path, journal: WriteJournal
    ) -> List[Path]:
        game_root = Path(project.game_root) if project.game_root else None
        if game_root is None or not self.is_renpy_game(game_root):
            raise GameError(
                f"project.game_root 无效或非 Ren'Py 游戏目录：{project.game_root!r}"
            )
        work_dir = Path(work_dir)
        output_dir = work_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _timestamp()
        bak_root = work_dir / "bak" / "game" / timestamp

        journal_id = journal.begin(f"write_back:{project.game_root}")
        parser = RenpyParser()
        written: List[Path] = []
        try:
            for gf in project.files:
                rel = gf.game_relpath
                if not rel:
                    continue
                src = _find_source(rel, work_dir, game_root / "game")
                parser.parse_file(src, game_relpath=rel)
                text = parser.regenerate(project, rel)
                if text == "":
                    # 该文件未被此 parser 实例解析（防御性回退：原样复制）
                    text = src.read_text(encoding="utf-8", errors="replace")

                dst = game_root / "game" / rel
                backup = None
                if dst.exists():
                    backup = bak_root / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, backup)
                    journal.record(journal_id, "write_back", src, dst, backup)
                else:
                    journal.record(journal_id, "write_back", src, dst, None)

                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(text, encoding="utf-8")
                # 保留原文件权限（§8.3）
                if backup is not None and backup.exists():
                    _copy_mode(backup, dst)

                # output/ 同时留副本（§8.3 / §2.2）
                out = output_dir / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text, encoding="utf-8")

                written.append(dst)

                # 提示：同名 .rpyc 需删除或重编译（§8.3 / §15.2）
                rpyc = dst.with_suffix(".rpyc")
                if rpyc.exists():
                    logger.warning(
                        "写回 %s，但存在同名编译产物 %s：请删除或让 Ren'Py 重编译，"
                        "否则游戏仍读取旧 .rpyc",
                        dst.relative_to(game_root),
                        rpyc.relative_to(game_root),
                    )
            journal.commit(journal_id)
            return written
        except Exception:
            # §15.2：任一失败整体回滚
            try:
                journal.rollback(journal_id)
            except Exception as rb:  # pragma: no cover - defensive
                logger.error("写回回滚失败：%s", rb)
            raise


# -- 模块级辅助 -------------------------------------------------------------


def _render_patch(template: str, hotkeys: dict[str, str]) -> str:
    text = template
    for key, default in _HOTKEY_DEFAULTS.items():
        value = hotkeys.get(key, default)
        text = text.replace("{" + key + "}", value)
    return text


def _find_source(rel: str, work_dir: Path, game_root: Path) -> Path:
    for cand in (work_dir / "output" / rel, work_dir / rel, game_root / rel):
        if cand.exists():
            return cand
    # 都不存在时返回 game_root 下路径，交由上层报错（文件缺失）
    return game_root / rel


def _copy_mode(src: Path, dst: Path) -> None:
    try:
        mode = os.stat(src).st_mode
        os.chmod(dst, mode)
    except OSError:  # pragma: no cover - 跨平台权限差异
        pass


def _timestamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _builtin_skeleton(game_root: Path, language: str) -> List[Path]:
    """内置近似 tl 骨架：扫描 game/*.rpy（排除 tl/）生成 translate 块（§8.5 回退）。

    覆盖长尾有限（设计 §15.5 第 3 点），仅作无 SDK 时的兜底。
    """
    game_root = Path(game_root)
    game_dir = game_root / "game"
    tl_dir = game_dir / "tl" / language
    tl_dir.mkdir(parents=True, exist_ok=True)
    produced: List[Path] = []
    if not game_dir.is_dir():
        return produced
    for rpy in sorted(game_dir.rglob("*.rpy")):
        if "tl" in rpy.parts:
            continue
        parser = RenpyParser()
        rel = str(rpy.relative_to(game_dir))
        segs = parser.parse_file(rpy, game_relpath=rel)
        translatable = [
            s
            for s in segs
            if s.kind in ("dialogue", "strings") and s.status != SegmentStatus.SKIPPED
        ]
        if not translatable:
            continue
        body: List[str] = []
        for s in translatable:
            label = f"{rpy.stem}_{s.uid}"
            body.append(f"translate {language} {label}:")
            if s.kind == "strings":
                body.append(f'    old "{s.source}"')
                body.append('    new ""')
            else:
                body.append(f'    "{s.source}"')
            body.append("")
        out = tl_dir / (rpy.stem + ".rpy")
        out.write_text("\n".join(body), encoding="utf-8")
        produced.append(out)
    logger.info("内置近似骨架生成 %d 个文件（语言=%s）", len(produced), language)
    return produced


def make_unren() -> UnrenImpl:
    """工厂：返回默认 Unren 实现（便于替换/测试）。"""
    return UnrenImpl()


__all__ = [
    "UnrenImpl",
    "make_unren",
    "PATCH_CONSOLE",
    "PATCH_QUICK",
    "PATCH_SKIP",
    "PATCH_ROLLBACK",
    "_PATCH_TEMPLATES",
    "_HOTKEY_DEFAULTS",
    "_builtin_skeleton",
]
