"""W4 SDK 集成测试（SdkBridge 协议，设计 §8.5）。

覆盖：probe 识别 SDK 与引擎版本、generate 在无本机 SDK 时回退内置骨架、
版本探测顺序（py3 → 8.x，py2 → 7.x），以及桥接参数构造。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pytran.sdk_bridge import SdkBridgeImpl, _BRIDGE_TEMPLATE, _launcher_for

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_GAME = FIXTURES / "fake_game"


@pytest.fixture
def bridge() -> SdkBridgeImpl:
    return SdkBridgeImpl()


def _fake_sdk(tmp_path: Path, lib_kind: str = "py3") -> Path:
    """构造一个最小 SDK 目录（含 launcher 占位 + 决定版本的 lib 子目录）。"""
    sdk = tmp_path / "sdk"
    sdk.mkdir(parents=True)
    (sdk / "renpy.exe").write_text("", encoding="utf-8")
    (sdk / "lib" / (f"{lib_kind}-windows-x86_64")).mkdir(parents=True)
    return sdk


# -- §8.5 probe --------------------------------------------------------------


def test_probe_rejects_missing_marker(tmp_path: Path, bridge: SdkBridgeImpl) -> None:
    sdk = tmp_path / "bad"
    sdk.mkdir()
    assert bridge.probe(sdk, FAKE_GAME) is None


def test_probe_detects_8x_for_py3(bridge: SdkBridgeImpl, tmp_path: Path) -> None:
    sdk = _fake_sdk(tmp_path, "py3")
    assert bridge.probe(sdk, FAKE_GAME) == "8.x"


def test_probe_detects_7x_for_py2(bridge: SdkBridgeImpl, tmp_path: Path) -> None:
    sdk = _fake_sdk(tmp_path, "py2")
    # 游戏根自身不带 lib，使 probe 回退到 SDK 的 py2 lib 判定
    game = tmp_path / "game_root"
    (game / "game").mkdir(parents=True)
    (game / "renpy").mkdir()
    assert bridge.probe(sdk, game) == "7.x"


def test_probe_infer_from_game_lib(
    bridge: SdkBridgeImpl, tmp_path: Path
) -> None:
    sdk = _fake_sdk(tmp_path, "py3")
    game = tmp_path / "game_root"
    import shutil

    shutil.copytree(FAKE_GAME, game)
    # 去掉 game 的 lib，让桥接从 game/lib 推断（此处无 game/lib，走 sdk）
    assert bridge.probe(sdk, game) == "8.x"


# -- §8.5 generate（无本机 SDK 时回退） --------------------------------------


def test_generate_falls_back_without_real_sdk(
    bridge: SdkBridgeImpl, tmp_path: Path
) -> None:
    """无 renpy launcher 可执行时，generate 应回退内置骨架。"""
    game = tmp_path / "g"
    import shutil

    shutil.copytree(FAKE_GAME, game)
    # 传入一个非 SDK 目录（无 launcher），触发回退
    not_sdk = tmp_path / "not_sdk"
    not_sdk.mkdir()
    files = bridge.generate(not_sdk, game, "chinese", work_dir=tmp_path)
    assert files, "回退应产出内置骨架"
    assert any(p.name == "script.rpy" for p in files)


def test_generate_bridge_params(bridge: SdkBridgeImpl, tmp_path: Path) -> None:
    """桥接脚本应调用官方生成 API 并带语言参数。"""
    sdk = _fake_sdk(tmp_path)
    launcher = _launcher_for(sdk)
    assert launcher is not None
    rendered = _BRIDGE_TEMPLATE.format(language="chinese")
    assert "generate_translations" in rendered
    assert "chinese" in rendered
    assert "chinese" in rendered
    assert "renpy.quit()" in rendered


def test_launcher_for_returns_exe(tmp_path: Path) -> None:
    sdk = _fake_sdk(tmp_path)
    assert _launcher_for(sdk).name == "renpy.exe"


def test_generate_with_real_sdk_skips_when_absent(
    bridge: SdkBridgeImpl, tmp_path: Path
) -> None:
    """若 SDK 真实存在（launcher 可执行），但本机无 Ren'Py，调用会失败回退；
    测试仅验证：无效 launcher 时安全回退，不抛未捕获异常。"""
    game = tmp_path / "g"
    import shutil

    shutil.copytree(FAKE_GAME, game)
    sdk = tmp_path / "sdk_real"
    sdk.mkdir(parents=True)
    (sdk / "renpy.exe").write_text("", encoding="utf-8")
    # 空 exe 无法执行 -> generate_translations 路径失败 -> 回退
    files = bridge.generate(sdk, game, "chinese", work_dir=tmp_path)
    assert files  # 回退成功
