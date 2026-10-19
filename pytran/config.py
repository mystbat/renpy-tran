"""配置（契约文件，T0 冻结 —— 任何会话不得修改）。

设计文档 §2.1（config.py 职责）、§8.5（renpy_sdk_dir）、§11（key 安全）、§14（fallback_model）。
读取顺序：环境变量 TL_API_KEY > config.ini（§12）。
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_ENV_KEY = "TL_API_KEY"
DEFAULT_CONFIG_NAME = "config.ini"

_DEFAULT_INI = """[API]
url = https://api.siliconflow.cn/v1
api_key =

[Model]
model =
fallback_model =
max_tokens = 2048
temperature = 0.2
top_p = 0.9

[Engine]
max_workers = 3
timeout = 120
max_retry_rounds = 3
round_budget_cap = 5
keep_declare_rate_limit = 0.15
keep_declare_min_batch = 5
keep_declare_min_total = 20

[Game]
language = chinese
renpy_sdk_dir =
"""

_INI_SECTION_MAP: dict[tuple[str, str], str] = {
    ("API", "url"): "api_url",
    ("API", "api_key"): "api_key",
    ("Model", "model"): "model",
    ("Model", "fallback_model"): "fallback_model",
    ("Model", "max_tokens"): "max_tokens",
    ("Model", "temperature"): "temperature",
    ("Model", "top_p"): "top_p",
    ("Engine", "max_workers"): "max_workers",
    ("Engine", "timeout"): "timeout",
    ("Engine", "max_retry_rounds"): "max_retry_rounds",
    ("Engine", "round_budget_cap"): "round_budget_cap",
    ("Engine", "keep_declare_rate_limit"): "keep_declare_rate_limit",
    ("Engine", "keep_declare_min_batch"): "keep_declare_min_batch",
    ("Engine", "keep_declare_min_total"): "keep_declare_min_total",
    ("Game", "language"): "language",
    ("Game", "renpy_sdk_dir"): "renpy_sdk_dir",
}


@dataclass
class Config:
    """全局配置。字段即契约：引擎/Provider/游戏集成模块从这里取参。"""

    api_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    model: str = ""
    fallback_model: str = ""             # §5.3 第 2 次定点升级时切换；空 = 不切换
    max_tokens: int = 2048
    temperature: float = 0.2
    top_p: float = 0.9
    max_workers: int = 3                 # §4.3 并发，config 可调
    timeout: float = 120.0               # 请求超时秒（§4.1）
    max_retry_rounds: int = 3            # 重译轮上限（config 不得超过 round_budget_cap，§5.3）
    round_budget_cap: int = 5            # 代码硬上限（§5.3 无条件终止线）
    keep_declare_rate_limit: float = 0.15  # §5.4 申报率阈值（ADR-0006 双口径）
    keep_declare_min_batch: int = 5      # §5.4 单批降级的最小样本量（ADR-0006）
    keep_declare_min_total: int = 20     # §5.4 累计闸的最小样本量（ADR-0006）
    language: str = "chinese"            # tl 目录语言名（§8.3）
    renpy_sdk_dir: str = ""              # §8.5；空 = 未配置
    profile_path: str = "profile.json"   # §7.2
    work_dir: str = "."                  # 工作目录（§2.2）

    def effective_max_retry_rounds(self) -> int:
        """max_retry_rounds 被 round_budget_cap 钳制（§5.3：config 不得超硬上限）。"""
        return min(self.max_retry_rounds, self.round_budget_cap)

    def sdk_dir(self) -> Optional[Path]:
        """返回有效 SDK 目录（存在且含 launcher 标志），否则 None（§8.5 校验）。"""
        if not self.renpy_sdk_dir:
            return None
        p = Path(self.renpy_sdk_dir)
        if not p.is_dir():
            return None
        markers = ("renpy.exe", "renpy.sh", "renpy.py")
        if not any((p / m).exists() for m in markers):
            return None
        return p

    @classmethod
    def load(cls, work_dir: Path | str = ".") -> "Config":
        """从 work_dir/config.ini 读取；文件不存在时写出默认模板再读。环境变量 TL_API_KEY 优先。"""
        work = Path(work_dir)
        ini_path = work / DEFAULT_CONFIG_NAME
        if not ini_path.exists():
            ini_path.write_text(_DEFAULT_INI, encoding="utf-8")
        cp = configparser.ConfigParser()
        cp.read(ini_path, encoding="utf-8")
        kwargs: dict = {"work_dir": str(work)}
        for (section, option), attr in _INI_SECTION_MAP.items():
            if cp.has_section(section) and cp.has_option(section, option):
                kwargs[attr] = cp.get(section, option).strip()
        cfg = cls(**kwargs)
        env_key = os.environ.get(CONFIG_ENV_KEY, "").strip()
        if env_key:
            cfg.api_key = env_key
        cfg._coerce_types()
        return cfg

    def save(self, work_dir: Path | str | None = None) -> None:
        """写回 config.ini（含 api_key——文件在 .gitignore，红线是'不入 git'而非'不入本机配置'）。"""
        work = Path(work_dir) if work_dir else Path(self.work_dir)
        ini_path = work / DEFAULT_CONFIG_NAME
        cp = configparser.ConfigParser()
        if ini_path.exists():
            cp.read(ini_path, encoding="utf-8")
        for (section, option), attr in _INI_SECTION_MAP.items():
            if not cp.has_section(section):
                cp.add_section(section)
            cp.set(section, option, str(getattr(self, attr)))
        with ini_path.open("w", encoding="utf-8") as f:
            cp.write(f)

    def _coerce_types(self) -> None:
        for int_attr in ("max_tokens", "max_workers", "max_retry_rounds", "round_budget_cap",
                         "keep_declare_min_batch", "keep_declare_min_total"):
            try:
                setattr(self, int_attr, int(getattr(self, int_attr)))
            except (TypeError, ValueError):
                pass
        for float_attr in ("temperature", "top_p", "timeout", "keep_declare_rate_limit"):
            try:
                setattr(self, float_attr, float(getattr(self, float_attr)))
            except (TypeError, ValueError):
                pass


@dataclass
class TermProfile:
    """术语表 profile（§7.2）。terms.py 生成骨架，UI 可编辑。"""

    game: str = ""
    terms: dict[str, str] = field(default_factory=dict)          # 原文 → 译名
    exempt_english: list[str] = field(default_factory=list)      # 免翻英文短语（§5.1/§5.3）
    prompt_overrides: dict[str, str] = field(default_factory=dict)  # 如 system_append

    @classmethod
    def load(cls, path: Path | str) -> "TermProfile":
        import json
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(
            game=data.get("game", ""),
            terms={k: str(v) for k, v in data.get("terms", {}).items()},
            exempt_english=[str(x) for x in data.get("exempt_english", [])],
            prompt_overrides=dict(data.get("prompt_overrides", {})),
        )

    def save(self, path: Path | str) -> None:
        import json
        Path(path).write_text(
            json.dumps(
                {
                    "game": self.game,
                    "terms": self.terms,
                    "exempt_english": self.exempt_english,
                    "prompt_overrides": self.prompt_overrides,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
