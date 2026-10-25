"""配置（契约文件，T0 冻结 —— 任何会话不得修改）。

设计文档 §2.1（config.py 职责）、§8.5（renpy_sdk_dir）、§11（key 安全）、§14（fallback_model）。
读取顺序：环境变量 TL_API_KEY > config.ini（§12）。

变更记录：
- 2026-10-24 经总控批准新增 ``[Review]`` 段 4 字段（``review_enabled`` /
  ``review_model`` / ``review_window_lines`` / ``review_max_tokens``），用于
  「翻译后全文复核」（AI 校对）阶段取代已删除的 V7 批次指纹检测
  （见 docs/FEATURE-ai-review.md）。**仅新增，未改动任何既有字段语义**；
  按 docs/W5c L127 惯例，config.py 加字段需总控确认，本条目即为该确认。
- 2026-10-25 经总控批准新增 ``[Model]`` 段 1 字段 ``enable_thinking``
  （思维链开关，默认 ``true`` = 维持既有行为）。**仅新增，未改动任何
  既有字段语义**；缺省值使旧 config.ini 无需迁移即可继续工作。
  实测依据（SiliconFlow）：Qwen3-8B 默认输出 85 reasoning tokens /
  4.4s，置 ``false`` 后 0 reasoning / 0.5s，即推理链吃掉约 87% 输出
  预算——这正是 TPM 限流雪崩的放大源。注意该开关只对混合推理模型
  （Qwen3 系列）生效，纯推理模型（DeepSeek-R1）不响应。
- 2026-10-25 经总控批准新增 ``[Engine]`` 段 1 字段
  ``fallback_after_failures``（主模型连续失败多少次后切回退模型；
  ``0`` = 永不回退，为默认值 = 维持既有行为）。**仅新增，未改动任何
  既有字段语义**。背景：既有 STUCK 三档要连续 3 轮哈希相同才升到
  「切备用模型」档，而 ``round_budget_cap`` 只有 5，实测回退模型
  只拿到 **1 次**尝试就撞轮数上限被判死——等于「有回退但形同虚设」。
  新字段让用户按失败次数直接切，绕开三档的轮次消耗。
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_ENV_KEY = "TL_API_KEY"
DEFAULT_CONFIG_NAME = "config.ini"

# configparser BOOLEAN_STATES 口径（getboolean 的合法取值）。
_TRUE_STRINGS = {"1", "yes", "true", "on"}
_FALSE_STRINGS = {"0", "no", "false", "off"}


def _coerce_bool(value, default: bool = False) -> bool:
    """把 ini 字符串 / 任意值收敛成 bool；无法识别时返回 ``default``。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    return default

_DEFAULT_INI = """[API]
url = https://api.siliconflow.cn/v1
api_key =

[Model]
model =
fallback_model =
max_tokens = 2048
temperature = 0.2
top_p = 0.9
enable_thinking = true

[Engine]
max_workers = 3
timeout = 120
max_retry_rounds = 3
round_budget_cap = 5
fallback_after_failures = 0
keep_declare_rate_limit = 0.15
keep_declare_min_batch = 5
keep_declare_min_total = 20

[Game]
language = chinese
context_lines = 3
block_lines = 30
renpy_sdk_dir =

[Terms]
term_extract_use_ai = true

[Review]
review_enabled = true
review_model = deepseek-ai/DeepSeek-R1-0528-Qwen3-8B
review_window_lines = 2000
review_max_tokens = 8192
"""

_INI_SECTION_MAP: dict[tuple[str, str], str] = {
    ("API", "url"): "api_url",
    ("API", "api_key"): "api_key",
    ("Model", "model"): "model",
    ("Model", "fallback_model"): "fallback_model",
    ("Model", "max_tokens"): "max_tokens",
    ("Model", "temperature"): "temperature",
    ("Model", "top_p"): "top_p",
    ("Model", "enable_thinking"): "enable_thinking",
    ("Engine", "max_workers"): "max_workers",
    ("Engine", "timeout"): "timeout",
    ("Engine", "max_retry_rounds"): "max_retry_rounds",
    ("Engine", "round_budget_cap"): "round_budget_cap",
    ("Engine", "fallback_after_failures"): "fallback_after_failures",
    ("Engine", "keep_declare_rate_limit"): "keep_declare_rate_limit",
    ("Engine", "keep_declare_min_batch"): "keep_declare_min_batch",
    ("Engine", "keep_declare_min_total"): "keep_declare_min_total",
    ("Game", "language"): "language",
    ("Game", "context_lines"): "context_lines",
    ("Game", "block_lines"): "block_lines",
    ("Game", "renpy_sdk_dir"): "renpy_sdk_dir",
    ("Terms", "term_extract_use_ai"): "term_extract_use_ai",
    ("Review", "review_enabled"): "review_enabled",
    ("Review", "review_model"): "review_model",
    ("Review", "review_window_lines"): "review_window_lines",
    ("Review", "review_max_tokens"): "review_max_tokens",
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
    # 思维链开关（2026-10-25 新增）：True=允许模型输出推理链（既有行为），
    # False=请求体带 extra_body.enable_thinking=False 抑制推理。
    # 只影响**翻译**请求；复核请求不走此开关。
    enable_thinking: bool = True
    max_workers: int = 3                 # §4.3 并发，config 可调
    timeout: float = 120.0               # 请求超时秒（§4.1）
    max_retry_rounds: int = 3            # 重译轮上限（config 不得超过 round_budget_cap，§5.3）
    round_budget_cap: int = 5
    # 主模型连续失败多少次后切回退模型（2026-10-25 新增）。
    # 0 = 永不回退（默认，维持既有行为）；需同时配置 fallback_model 才生效。
    # 绕开 STUCK 三档的轮次消耗——三档要连续 3 轮哈希相同才切备用模型，
    # 而 round_budget_cap 只有 5，导致回退模型只剩 1 次机会。
    fallback_after_failures: int = 0            # 代码硬上限（§5.3 无条件终止线）
    keep_declare_rate_limit: float = 0.15  # §5.4 申报率阈值（ADR-0006 双口径）
    keep_declare_min_batch: int = 5      # §5.4 单批降级的最小样本量（ADR-0006）
    keep_declare_min_total: int = 20     # §5.4 累计闸的最小样本量（ADR-0006）
    context_lines: int = 3               # §4.1 前文参考行数（0=关闭）
    block_lines: int = 30                # §4.1 切块大小（行/块）
    language: str = "chinese"            # tl 目录语言名（§8.3）
    renpy_sdk_dir: str = ""              # §8.5；空 = 未配置
    term_extract_use_ai: bool = True     # W5c §2 信号源 C：AI 辅助识别术语
    profile_path: str = "profile.json"   # §7.2
    work_dir: str = "."                  # 工作目录（§2.2）

    # -- 复核阶段（AI 校对，取代已删除的 V7 批次指纹检测） ---------------------
    # 翻译完成后对全文做一次「原文-译文」对照复核，取代 V7 批次指纹猜测
    # （见 docs/FEATURE-ai-review.md）。
    # 窗口按行给：复核输入 ≈ N × (原文+译文)，与块大小无关，故窗口开大不额外
    # 增加输入成本；真正限制窗口的是**输出**预算（逐行确认标记 + 推理链），
    # 因此复核单独用 ``review_max_tokens``，不沿用翻译的 ``max_tokens``。
    review_enabled: bool = True          # 复核阶段开关
    review_model: str = "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"  # 空 = 沿用主模型
    review_window_lines: int = 2000      # 单次复核覆盖的行数
    review_max_tokens: int = 8192        # 复核专用输出上限（含推理链余量）

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
                         "keep_declare_min_batch", "keep_declare_min_total", "context_lines",
                         "block_lines", "review_window_lines", "review_max_tokens",
                         "fallback_after_failures"):
            try:
                setattr(self, int_attr, int(getattr(self, int_attr)))
            except (TypeError, ValueError):
                pass
        for float_attr in ("temperature", "top_p", "timeout", "keep_declare_rate_limit"):
            try:
                setattr(self, float_attr, float(getattr(self, float_attr)))
            except (TypeError, ValueError):
                pass
        # 布尔字段：ini 里写的是字符串，按 configparser 语义取真值集
        # （configparser 的 getboolean 接受 1/yes/true/on；这里保持同等口径）。
        self.term_extract_use_ai = _coerce_bool(
            getattr(self, "term_extract_use_ai", True), default=True)
        # 复核阶段开关（AI 校对）：默认开，与 ini 模板一致。
        self.review_enabled = _coerce_bool(
            getattr(self, "review_enabled", True), default=True)
        # 思维链开关：默认 True（=维持既有行为，旧 ini 缺此键时不改变行为）。
        self.enable_thinking = _coerce_bool(
            getattr(self, "enable_thinking", True), default=True)


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
