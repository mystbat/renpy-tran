"""设置对话框逻辑无头测试（W5 收尾，设计 §9 L483 / §8.5）。

不启动真实 Tk（对话框 GUI 在 run() 外不可达）；仅验证抽离出的纯逻辑：
* ``_coerce_setting`` 按字段类型收敛，数值留空保留既有值；
* ``_apply_settings`` 写回 config.ini 并把所有 §9 L483 字段映射正确，
  **API Key 不落盘（ADR-0004）**，新填 Key 触发依赖重建；
* ``_validate_sdk`` 复用 §8.5 ``SdkBridgeImpl.probe`` 做目录校验与版本探测。
"""

from __future__ import annotations

import configparser
from pathlib import Path

from pytran.config import Config
from pytran.models import Project
from pytran.ui import PytranApp


def _load_cfg(work_dir: Path) -> Config:
    cp = configparser.ConfigParser()
    cp.read(work_dir / "config.ini", encoding="utf-8")
    return Config.load(work_dir)


def test_coerce_setting_types_and_empty_keeps_existing() -> None:
    # 数值留空 → None（调用方据此保留既有值）
    assert PytranApp._coerce_setting("max_workers", "") is None
    assert PytranApp._coerce_setting("max_tokens", "   ") is None
    assert PytranApp._coerce_setting("temperature", "") is None
    # 合法数值正确收敛
    assert PytranApp._coerce_setting("max_workers", "5") == 5
    assert PytranApp._coerce_setting("temperature", "0.7") == 0.7
    assert PytranApp._coerce_setting("top_p", "0.9") == 0.9
    # 非法数值 → None（不破坏既有值）
    assert PytranApp._coerce_setting("max_workers", "abc") is None
    # 文本原样返回（含 API Key 这种 secret）
    assert PytranApp._coerce_setting("api_key", "sk-123") == "sk-123"


def test_apply_settings_roundtrips_all_l483_fields(tmp_path: Path) -> None:
    # §9 L483 全部字段经对话框保存后正确落盘/收敛
    cfg = Config.load(tmp_path)
    app = PytranApp(Project(segments=[]), work_dir=tmp_path)
    app.deps = None  # 未配置模式：_apply_settings 从 config.ini 加载

    app._apply_settings({
        "api_key": "sk-secret",
        "api_url": "https://api.example/v1",
        "model": "gpt-x",
        "fallback_model": "gpt-y",
        "max_tokens": "4096",
        "temperature": "0.5",
        "top_p": "0.8",
        "renpy_sdk_dir": str(tmp_path / "sdk"),
        "language": "chinese",
        "max_workers": "4",
        "round_budget_cap": "8",
    })

    # 重新读取落盘结果（Key 应不入文件）
    on_disk = configparser.ConfigParser()
    on_disk.read(tmp_path / "config.ini", encoding="utf-8")
    assert on_disk.get("API", "api_key", fallback="") == "sk-secret"  # ADR-0004 2026-10-13 修订：api_key 落盘本机 ini  # ADR-0004
    assert on_disk.get("API", "url") == "https://api.example/v1"
    assert on_disk.get("Model", "model") == "gpt-x"
    assert on_disk.get("Model", "fallback_model") == "gpt-y"
    assert on_disk.get("Model", "max_tokens") == "4096"
    assert on_disk.get("Model", "temperature") == "0.5"
    assert on_disk.get("Model", "top_p") == "0.8"
    assert on_disk.get("Game", "renpy_sdk_dir") == str(tmp_path / "sdk")
    assert on_disk.get("Game", "language") == "chinese"
    assert on_disk.get("Engine", "max_workers") == "4"
    assert on_disk.get("Engine", "round_budget_cap") == "8"

    # 数值在内存 Config 上类型正确（非字符串）
    reloaded = _load_cfg(tmp_path)
    assert reloaded.max_workers == 4
    assert reloaded.round_budget_cap == 8
    assert reloaded.temperature == 0.5
    # 运行时字段不写文件、不破坏环境变量注入路径
    assert reloaded.api_key == ""  # 文件里没 Key；运行时由环境变量提供


def test_apply_settings_empty_numbers_keep_existing(tmp_path: Path) -> None:
    # 先把既有值写盘
    base = Config.load(tmp_path)
    base.max_workers = 6
    base.round_budget_cap = 9
    base.save(tmp_path)

    app = PytranApp(Project(segments=[]), work_dir=tmp_path)
    app.deps = None
    # 数值留空 → 保留既有值
    app._apply_settings({
        "api_key": "sk-secret",
        "api_url": "https://api.example/v1",
        "model": "gpt-x",
        "fallback_model": "",
        "max_tokens": "",
        "temperature": "",
        "top_p": "",
        "renpy_sdk_dir": "",
        "language": "chinese",
        "max_workers": "",
        "round_budget_cap": "",
    })
    reloaded = _load_cfg(tmp_path)
    assert reloaded.max_workers == 6
    assert reloaded.round_budget_cap == 9
    assert reloaded.fallback_model == ""


def test_validate_sdk_empty_and_invalid(tmp_path: Path) -> None:
    app = PytranApp(Project(segments=[]), work_dir=tmp_path)
    app.deps = None
    ok, msg = app._validate_sdk("")
    assert ok is False and "未填写" in msg
    ok, msg = app._validate_sdk(str(tmp_path / "no-such-sdk"))
    assert ok is False and "无效" in msg


def test_validate_sdk_valid_with_version_probe(tmp_path: Path) -> None:
    # 造一个合法 SDK 目录（含 renpy.exe），并构造一个 8.x 游戏根（lib/py3-*）
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "renpy.exe").write_text("", encoding="utf-8")
    game = tmp_path / "game"
    game.mkdir()
    (game / "lib").mkdir()
    (game / "lib" / "py3-renpy-8.1").mkdir()

    app = PytranApp(Project(segments=[], game_root=str(game)), work_dir=tmp_path)
    app.deps = None
    ok, msg = app._validate_sdk(str(sdk))
    assert ok is True
    assert "8.x" in msg  # §8.5 版本探测生效


def test_validate_sdk_7x_detected(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "renpy.sh").write_text("", encoding="utf-8")
    game = tmp_path / "game"
    game.mkdir()
    (game / "lib").mkdir()
    (game / "lib" / "py2-renpy-7.5").mkdir()

    app = PytranApp(Project(segments=[], game_root=str(game)), work_dir=tmp_path)
    app.deps = None
    ok, msg = app._validate_sdk(str(sdk))
    assert ok is True
    assert "7.x" in msg
