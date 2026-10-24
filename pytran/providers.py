"""OpenAI 兼容客户端：超时/重试/429 退避（设计文档 §4.1、§4.3、§12、ADR-0004）。

设计取舍：
- 只用 OpenAI 官方 SDK 的标准调用形态（`chat.completions.create`），
  任何 OpenAI 兼容端点（vLLM / 硅基流动 / DeepSeek / 本地 ollama）换 `api_url` 即可；
- **不重试协议错误**（§4.3）：解析失败是模型输出问题，重试同一请求大概率再失败，
  交给引擎换"逐行错误原因"策略；网络错误/429/5xx 才退避重试；
- 日志只记模型名、编号区间与响应前 N 字符，**绝不记录 key / headers / 完整请求体**
  （§12、任务 W2 特别约束）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping, Sequence

from pytran.config import Config, TermProfile
from pytran.interfaces import ProviderError
from pytran.protocol import build_messages, build_request_params, parse_response

logger = logging.getLogger(__name__)

__all__ = ["OpenAIProvider", "TERM_EXTRACT_SYSTEM_PROMPT"]

# 术语抽取的信号 C 用的系统提示（W5c §2 信号源 C）：只要名字，不要解释。
TERM_EXTRACT_SYSTEM_PROMPT = (
    "You extract proper nouns from game text. Reply with one name per line, "
    "nothing else."
)

# 信号 C 的采样/超时口径（W5c §2：最多 1 次/次抽取、timeout 60s）。
TERM_EXTRACT_MAX_TOKENS = 512
TERM_EXTRACT_TIMEOUT = 60.0

# §4.3 退避序列（指数 2s/4s/8s）
BACKOFF_BASE = 2.0
BACKOFF_FACTOR = 2.0
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
_PREVIEW_CHARS = 120

# BUGFIX(timeout-amplify) 2026-10-24：超时口径。
# 用户实测（清空工程重新导入后点翻译）：进度条一直滑 → APITimeoutError
# → 「2s 后重试（1/2）」。两个叠加缺陷：
#   ① openai SDK 默认 max_retries=2，叠在本模块 _request_with_retry 之上，
#      一次逻辑尝试 = 3 × timeout ≈ 181.9s，而进度条此时是不确定态；
#   ② 固定 timeout=60s 对 50 行满块本就不够（实测推理模型 177~275s）。
# 对策：SDK 层重试显式关掉（重试只由 §4.3 的单层状态机负责），
#      超时按本次批量行数线性放大并设硬上限。
TIMEOUT_REF_LINES = 10   # 基准行数：≤10 行用 config.timeout 原值
TIMEOUT_MAX = 900.0      # 硬上限，避免异常配置把单次请求拖到不可控


class OpenAIProvider:
    """Provider 实现（interfaces.Provider）。`client` 可注入，便于测试替身。"""

    def __init__(
        self,
        config: Config,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._client = client
        if self._client is None:
            self._client = self._build_client(config)

    # -- 通用文本补全（非 Provider 契约） -------------------------------------
    def complete_text(self, prompt: str, system: str = "") -> str:
        """一次性文本补全（不走 JSONL 协议，W5c §2 信号源 C 用）。

        与 ``translate_batch`` 复用同一 client / 超时 / 脱敏口径，但：
        - 不经过 ``protocol`` 的 JSONL 解析（返回的是纯文本名字列表）；
        - 单次调用、**不重试**（信号 C 是增益不是依赖，失败即静默跳过）；
        - ``max_tokens`` 收紧到 512（名字列表不需要长输出）。

        任何失败都抛 ``ProviderError``——调用方（``terms._ai_candidates``）
        负责吞掉，不让它成为抽取失败的原因。
        """
        prompt = str(prompt or "")
        if not prompt.strip():
            return ""
        self._require_api_key()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        # 主模型未配置时退到 fallback_model；两者都空则由服务端报错上抛。
        model = self._config.model or self._config.fallback_model
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": TERM_EXTRACT_MAX_TOKENS,
            "timeout": TERM_EXTRACT_TIMEOUT,
        }
        logger.debug("术语抽取请求（prompt %d 字符）", len(prompt))
        try:
            response = self._client.chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001 - 统一转为 ProviderError
            raise ProviderError(
                f"术语抽取调用失败：{type(exc).__name__}"
            ) from exc
        content = self._extract_content(response)
        logger.debug("术语抽取响应预览：%s", self._preview(content))
        return content

    # -- 契约方法（interfaces.Provider） --------------------------------------
    def translate_batch(
        self,
        numbered_lines: list[tuple[int, str]],
        profile: TermProfile,
        context: list[tuple[str, str]] | list[tuple[int, str, str]] = (),
        allow_keep_declare: bool = False,
        use_fallback_model: bool = False,
    ) -> dict[int, str | Any]:
        """翻译一批编号行。返回 `{uid: 译文 或 KeepDeclared}`。

        `context` 接受 `(uid, source, target)`（推荐）；`(source, target)` 二元组
        也兼容（缺 uid 时按序补号，仅用于日志与提示词展示）。
        """
        if not numbered_lines:
            return {}
        self._require_api_key()

        ctx = self._normalize_context(context)
        messages = build_messages(numbered_lines, profile, ctx, allow_keep_declare)
        params = build_request_params(self._config, messages, use_fallback_model)
        # BUGFIX(timeout-amplify)：按本批行数放大超时，覆盖整块生成耗时。
        params["timeout"] = self._effective_timeout(len(numbered_lines))
        sources = {uid: text for uid, text in numbered_lines}
        ids = [uid for uid, _ in numbered_lines]

        raw = self._request_with_retry(params, ids)
        # 解析失败（ProtocolError）不重试——§4.3：协议错误交引擎换策略
        from pytran.interfaces import ProtocolError  # 局部导入避免循环依赖

        try:
            parsed = parse_response(
                raw,
                ids,
                allow_keep_declare=allow_keep_declare,
                sources=sources,
                keep_declare_rate_limit=self._config.keep_declare_rate_limit,
            )
        except ProtocolError:
            logger.warning("响应解析失败（不重试，交引擎改策略）：uid=%s..%s", ids[0], ids[-1])
            raise
        return dict(parsed.results)

    # -- 内部 -----------------------------------------------------------------
    def _effective_timeout(self, line_count: int) -> float:
        """按本次批量行数放大超时（BUGFIX(timeout-amplify)）。

        `config.timeout` 是「10 行基准块」的口径；块越大，单次生成越久。
        向上取整到 TIMEOUT_REF_LINES 的整数倍，并夹在 [base, TIMEOUT_MAX]。
        `timeout <= 0` 视为「不限」，直接给硬上限。
        """
        base = float(self._config.timeout)
        if base <= 0:
            return TIMEOUT_MAX
        lines = max(0, int(line_count))
        batches = (lines + TIMEOUT_REF_LINES - 1) // TIMEOUT_REF_LINES
        return min(TIMEOUT_MAX, max(base, base * batches))

    def _require_api_key(self) -> None:
        # ADR-0004：key 只来自环境/配置注入；缺失时给出可操作提示，不回显任何值
        if not getattr(self._config, "api_key", ""):
            raise ProviderError(
                "缺少 API Key：请设置环境变量 TL_API_KEY（或 PYTRAN_API_KEY）后重试"
            )

    @staticmethod
    def _normalize_context(
        context: Sequence[Any],
    ) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        for idx, item in enumerate(context):
            if len(item) == 3:
                uid, src, dst = item
                out.append((int(uid), str(src), str(dst)))
            else:
                src, dst = item
                out.append((idx, str(src), str(dst)))
        return out

    def _request_with_retry(self, params: Mapping[str, Any], ids: Sequence[int]) -> str:
        attempts = max(1, int(getattr(self._config, "effective_max_retry_rounds", None)
                              and self._config.effective_max_retry_rounds() or 1) or 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                logger.debug(
                    "请求 model=%s uid=%s..%s 共%d行（第%d次）",
                    params.get("model"), ids[0], ids[-1], len(ids), attempt,
                )
                response = self._client.chat.completions.create(**params)
                content = self._extract_content(response)
                logger.debug("响应预览：%s", self._preview(content))
                return content
            except Exception as exc:  # noqa: BLE001 - 第三方异常类型不可枚举
                if not self._is_retryable(exc):
                    # §4.3：非可重试错误（含鉴权/参数错误）立即上抛
                    raise ProviderError(f"调用失败（不可重试）：{type(exc).__name__}") from exc
                last_error = exc
                if attempt >= attempts:
                    break
                delay = BACKOFF_BASE * (BACKOFF_FACTOR ** (attempt - 1))
                logger.warning(
                    "调用失败（%s），%.0fs 后重试（%d/%d）", type(exc).__name__, delay, attempt, attempts
                )
                self._sleep(delay)
        raise ProviderError(
            f"重试 {attempts} 次仍失败：{type(last_error).__name__ if last_error else 'unknown'}"
        )

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """429/5xx/超时/连接错误 → 可重试；其它（400 参数错、401 鉴权）→ 不重试。"""
        status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
        if status in _RETRYABLE_STATUS:
            return True
        name = type(exc).__name__.lower()
        if any(k in name for k in ("ratelimit", "timeout", "connection", "apiconnection",
                                   "internalserver", "apierror")):
            return True
        code = str(getattr(exc, "code", "") or getattr(exc, "type", "") or "").lower()
        return any(k in code for k in ("rate_limit", "timeout", "server_error", "overloaded"))

    @staticmethod
    def _extract_content(response: Any) -> str:
        """OpenAI 标准响应：`choices[0].message.content`；也兼容纯 dict 返回。"""
        if isinstance(response, Mapping):
            choices = response.get("choices") or []
            if choices:
                return str((choices[0].get("message") or {}).get("content") or "")
            return ""
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        return str(getattr(choices[0].message, "content", "") or "")

    @staticmethod
    def _preview(content: str) -> str:
        """§12：只记前 N 字符，避免日志体积与敏感内容扩散。"""
        text = (content or "").replace("\n", "\\n")
        return text[:_PREVIEW_CHARS] + ("…" if len(text) > _PREVIEW_CHARS else "")

    @staticmethod
    def _build_client(config: Config) -> Any:
        """延迟导入 openai：无 SDK 时不阻断其它模块的导入与测试。"""
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise ProviderError("未安装 openai SDK：pip install openai>=1.30") from exc
        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": float(config.timeout),
            # BUGFIX(timeout-amplify)：SDK 自带重试必须关掉，否则
            # 一次逻辑尝试 = (max_retries+1) × timeout，把 §4.3 的单层
            # 重试放大成 3 倍，用户侧表现为「进度条一直滑」。
            "max_retries": 0,
        }
        if getattr(config, "api_url", ""):
            kwargs["base_url"] = config.api_url
        return OpenAI(**kwargs)
