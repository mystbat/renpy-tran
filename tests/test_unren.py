"""W4 游戏集成测试（Unren 协议，设计 §8 / §8.4 / §15.2）。

覆盖：结构检测、解释器探测、RPA 解包、rpyc 反编译、补丁注入/卸载（幂等）、
骨架生成（内置回退）、事务化写回（备份 + journal + 回滚）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pytran.config import Config
from pytran.interfaces import GameError
from pytran.journal import JsonWriteJournal
from pytran.models import GameFile, Project, Segment, SegmentStatus
from pytran.parser import RenpyParser
from pytran.project import build_project
from pytran.unren import (
    PATCH_CONSOLE,
    PATCH_QUICK,
    PATCH_ROLLBACK,
    PATCH_SKIP,
    UnrenImpl,
    _HOTKEY_DEFAULTS,
    _builtin_skeleton,
    _PATCH_TEMPLATES,
    _render_patch,
)

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_GAME = FIXTURES / "fake_game"


@pytest.fixture
def unren() -> UnrenImpl:
    return UnrenImpl()


# -- §8.4 补丁模板渲染 -------------------------------------------------------


def test_patch_templates_render_with_default_hotkeys() -> None:
    """四个补丁模板渲染后包含默认热键占位替换结果。"""
    for name, tmpl in _PATCH_TEMPLATES.items():
        text = _render_patch(tmpl, {})
        # 默认热键应被替换（无残留占位符）
        assert "{" not in text, f"{name} 残留占位符"
    # 具体断言：skip 模板用默认 TAB 键
    rendered = _render_patch(PATCH_SKIP, {})
    assert _HOTKEY_DEFAULTS["KEY_SKIP"] in rendered
    # console 模板不含热键占位，应原样保留
    assert "config.console" in PATCH_CONSOLE


def test_patch_hotkeys_override() -> None:
    """apply_patch 的 hotkeys 参数应替换默认热键。"""
    rendered = _render_patch(PATCH_QUICK, {"KEY_QUICKSAVE": "K_F2"})
    assert "K_F2" in rendered
    # 未覆盖的占位仍用默认
    assert _HOTKEY_DEFAULTS["KEY_QUICKLOAD"] in rendered


# -- §8.2 结构检测 / 解释器探测 ----------------------------------------------


def test_is_renpy_game_true_for_fixture() -> None:
    assert UnrenImpl().is_renpy_game(FAKE_GAME) is True


def test_is_renpy_game_false_for_plain_dir(tmp_path: Path) -> None:
    (tmp_path / "no_game_here").mkdir()
    assert UnrenImpl().is_renpy_game(tmp_path / "no_game_here") is False


def test_detect_game_python_orders_py3_first() -> None:
    ud = UnrenImpl()
    found = ud.detect_game_python(FAKE_GAME)
    assert found is not None
    assert "py3-windows-x86_64" in str(found)
    assert found.name in ("python.exe", "pythonw.exe", "python")


def test_detect_game_python_missing_lib(tmp_path: Path) -> None:
    (tmp_path / "game").mkdir()
    (tmp_path / "renpy").mkdir()
    assert UnrenImpl().detect_game_python(tmp_path) is None


def test_is_renpy_game_rejects_non_game(unren: UnrenImpl) -> None:
    with pytest.raises(GameError):
        unren.extract_rpa(FAKE_GAME.parent)  # 父目录不是游戏
    with pytest.raises(GameError):
        unren.decompile(FAKE_GAME.parent)


# -- §8.1 解包 / 反编译 ------------------------------------------------------


def test_extract_rpa(unren: UnrenImpl, tmp_path: Path) -> None:
    game = tmp_path / "game_root"
    # 复制 fake_game 并删除已解包产物，避免干扰
    import shutil

    shutil.copytree(FAKE_GAME, game)
    # 清掉非 RPA 的 rpy/rpyc，仅测 RPA
    for f in list(game.glob("game/*.rpy")) + list(game.glob("game/*.rpyc")):
        f.unlink()
    extracted = unren.extract_rpa(game)
    assert extracted, "应解包出 packed.rpy"
    assert any(p.name == "packed.rpy" for p in extracted)


def test_extract_rpa_remove_flag(unren: UnrenImpl, tmp_path: Path) -> None:
    import shutil

    game = tmp_path / "game_root"
    shutil.copytree(FAKE_GAME, game)
    for f in list(game.glob("game/*.rpy")) + list(game.glob("game/*.rpyc")):
        f.unlink()
    unren.extract_rpa(game, remove_rpa=True)
    assert not list(game.glob("game/*.rpa"))


def test_decompile_recovers_rpy(unren: UnrenImpl) -> None:
    out = unren.decompile(FAKE_GAME)
    assert any(p.name == "script.rpy" for p in out)
    recovered = FAKE_GAME / "game" / "script.rpy"
    assert "Hello, world." in recovered.read_text(encoding="utf-8")


def test_decompile_try_harder_noop_for_fake(unren: UnrenImpl) -> None:
    out = unren.decompile(FAKE_GAME, try_harder=True)
    assert any(p.name == "script.rpy" for p in out)


# -- §8.4 补丁注入 / 卸载 ----------------------------------------------------


@pytest.mark.parametrize("patch", ["console", "quick", "skip", "rollback"])
def test_apply_patch_writes_unren_rpy(unren: UnrenImpl, patch: str, tmp_path: Path) -> None:
    import shutil

    game = tmp_path / "g"
    shutil.copytree(FAKE_GAME, game)
    dst = unren.apply_patch(game, patch)
    assert dst.name == f"unren-{patch}.rpy"
    assert dst.exists()
    assert "config" in dst.read_text(encoding="utf-8")


def test_apply_patch_rejects_existing(unren: UnrenImpl, tmp_path: Path) -> None:
    import shutil

    game = tmp_path / "g"
    shutil.copytree(FAKE_GAME, game)
    unren.apply_patch(game, "console")
    with pytest.raises(GameError):
        unren.apply_patch(game, "console")


def test_remove_patches_idempotent(unren: UnrenImpl, tmp_path: Path) -> None:
    import shutil

    game = tmp_path / "g"
    shutil.copytree(FAKE_GAME, game)
    # 无补丁时返回空且安全
    assert unren.remove_patches(game) == []
    # 注入后再卸载
    unren.apply_patch(game, "skip")
    unren.apply_patch(game, "rollback")
    removed = unren.remove_patches(game)
    assert len(removed) == 2
    assert unren.remove_patches(game) == []  # 再调用幂等


# -- §8.3 / §8.5 骨架生成（内置回退） ----------------------------------------


def test_generate_skeleton_fallback(unren: UnrenImpl, tmp_path: Path) -> None:
    import shutil

    game = tmp_path / "g"
    shutil.copytree(FAKE_GAME, game)
    cfg = Config()
    files = unren.generate_skeleton(game, "chinese", cfg)
    # 无 SDK 时应回退内置骨架，至少覆盖主脚本
    assert files, "内置骨架应产出文件"
    assert any(p.name == "script.rpy" for p in files)
    content = (game / "game" / "tl" / "chinese" / "script.rpy").read_text(
        encoding="utf-8"
    )
    assert "translate chinese" in content


def test_builtin_skeleton_excludes_tl_dir(tmp_path: Path) -> None:
    """内置骨架只扫描源码 *.rpy，并排除已存在的 tl/ 翻译目录。

    - tl/ 下的既有译文不得被当作待翻译源文件；
    - 既有的 tl/chinese/already.rpy 必须原样保留，不被骨架覆盖。
    """
    game = tmp_path / "g"
    (game / "game").mkdir(parents=True)
    (game / "game" / "script.rpy").write_text(
        'translate chinese x:\n    "hi"\n', encoding="utf-8"
    )
    old = game / "game" / "tl" / "chinese" / "already.rpy"
    old.parent.mkdir(parents=True)
    old.write_text('translate chinese x:\n    "old"\n', encoding="utf-8")

    out = _builtin_skeleton(game, "chinese")

    names = {p.name for p in out}
    assert "script.rpy" in names  # 源 script.rpy 生成骨架
    assert "already.rpy" not in names  # tl/ 既有译文不是生成目标
    # 既有译文原样保留
    assert old.read_text(encoding="utf-8") == 'translate chinese x:\n    "old"\n'


# -- §8.3 / §15.2 事务化写回 ------------------------------------------------


def _make_project(game_root: str, rel: str, target: str) -> Project:
    segs = [
        Segment(
            uid=1,
            file=rel,
            locator=f"# game/{rel}:2",
            feature="translate chinese x_1:",
            kind="dialogue",
            comment="",
            role="",
            source="Hello, world.",
            target=target,
            status=SegmentStatus.TRANSLATED,
            attempts=1,
            error="",
            indent=4,
        )
    ]
    return Project(
        version=3,
        app="pytran",
        files=[GameFile(name=rel.split("/")[-1], game_relpath=rel)],
        next_uid=2,
        segments=segs,
        term_profile="profile.json",
        game_root=game_root,
    )


def test_write_back_writes_and_backs_up(unren: UnrenImpl, work_dir: Path) -> None:
    import shutil

    game = work_dir / "game_root"
    shutil.copytree(FAKE_GAME, game)
    # 准备 output 源：把 script.rpy 放 output 供 write_back 取用
    (work_dir / "output" / "script.rpy").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "output" / "script.rpy").write_text(
        'translate chinese start_1:\n    "Hello, world."\n    "This is a fake Ren\'Py game for pytran testing."\n',
        encoding="utf-8",
    )
    proj = _make_project(str(game), "script.rpy", "你好，世界。")
    journal = JsonWriteJournal(work_dir)
    written = unren.write_back(proj, work_dir, journal)
    assert written
    dst = game / "game" / "script.rpy"
    assert "你好，世界。" in dst.read_text(encoding="utf-8")
    # 备份应存在（bak/game/<timestamp>/script.rpy）
    backups = list((work_dir / "bak" / "game").rglob("script.rpy"))
    assert backups
    assert backups[0].read_text(encoding="utf-8") != dst.read_text(encoding="utf-8")
    # output 副本
    assert (work_dir / "output" / "script.rpy").exists()


def test_write_back_rolls_back_on_failure(
    unren: UnrenImpl, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    game = work_dir / "game_root"
    shutil.copytree(FAKE_GAME, game)
    (work_dir / "output" / "script.rpy").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "output" / "script.rpy").write_text(
        'translate chinese start_1:\n    "Hello, world."\n', encoding="utf-8"
    )
    original = (game / "game" / "script.rpy").read_text(encoding="utf-8")

    # 让 regenerate 抛错以触发回滚
    def boom(self, project, file_name):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(RenpyParser, "regenerate", boom)
    proj = _make_project(str(game), "script.rpy", "X")
    journal = JsonWriteJournal(work_dir)
    with pytest.raises(RuntimeError):
        unren.write_back(proj, work_dir, journal)
    # 游戏文件应恢复原样（回滚）
    assert (game / "game" / "script.rpy").read_text(encoding="utf-8") == original
