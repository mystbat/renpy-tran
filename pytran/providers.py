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

# BUGFIX(429-storm) 2026-10-25：限流退避口径。
# 用户实测日志：每块 `调用失败（RateLimitError），2s 后重试（1/2）`，
# 随即 `translate_batch raised; marking requested rows missing` →
# ProviderError「重试 2 次仍失败：RateLimitError」，整轮所有行判失败，
# 下一轮重发又撞限流，形成雪崩。
# 实测服务端响应（SiliconFlow）：
#   status=429 code=50602 body={"message":"...TPM limit reached."}
#   **且不带 Retry-After / x-ratelimit-* 任何头** —— 无法按服务端指示等待。
# TPM（每分钟 token）窗口的恢复尺度是**几十秒**，而 2s 退避对限流
# 等于立即重试：两次尝试都在同一窗口内，必然再次 429。
# 对策：限流单独用更长的基础间隔与上限，且**不消耗**普通的尝试次数口径
# 之外的额外预算（仍受 attempts 约束，保持 §4.3 单层状态机语义）。
RATE_LIMIT_BACKOFF_BASE = 20.0   # 429 首次退避 20s（覆盖 TPM 窗口量级）
RATE_LIMIT_BACKOFF_MAX = 120.0   # 429 单次退避上限，避免无限拉长

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

# 全文复核（review）的超时口径：窗口可达 2000 行，输出量远大于翻译批次，
# 沿用 TIMEOUT_MAX=900s 仍可能不够（R1 类推理模型逐行回执 2000 行 ≈ 5000
# token 生成）。复核是**增益操作**：超时就少复核一些，因此给独立上限。
REVIEW_TIMEOUT_MAX = 1800.0


class OpenAIProvider:
    """Provider 实现（interfaces.Provider）。`client` 可注入，便于测试替身。"""

    def __init__(
        self,
        config: Config,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_progress: "Callable[[str], None] | None" = None,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._client = client
        self._on_progress = on_progress
        if self._client is None:
            self._client = self._build_client(config)

    def reload_config(self, config: Config) -> None:
        """配置热更新（BUGFIX settings-live-reload, 2026-10-29）。

        设置界面保存后调用：换 API Key / 模型 / URL 无需重启程序。
        重建底层 OpenAI 客户端（旧 client 由 GC 回收）。

        测试注入的自定义 client（非 OpenAIProvider 构造路径）也一并替换
        为按新 config 构建的 client——调用方（ui._apply_settings）负责
        判断是否需要热更新；本方法总是执行。
        """
        self._config = config
        self._client = self._build_client(config)
        logger.info("provider 配置已热更新（model=%s, key=***)",
                    getattr(config, "model", "?"))

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

    # -- 全文复核（review） ---------------------------------------------------
    def review_text(
        self,
        prompt: str,
        system: str = "",
        *,
        max_tokens: int | None = None,
        line_count: int = 0,
    ) -> str:
        """全文复核通道：走复核模型 + 复核输出上限，单次请求、可重试。

        与 ``complete_text`` 的区别（W5h 后续 / 用户 2026-10-24 决策）：

        * 模型取 ``config.review_model``（空则退 ``config.model``）——
          复核是「另一个人再看一遍」，与翻译模型解耦；
        * ``max_tokens`` 取 ``config.review_max_tokens``——复核每行都要回执，
          输出量远大于术语抽取，512 一定截断；
        * 超时按本次窗口行数放大（复用 ``_effective_timeout``）——2000 行
          窗口的生成时间远不止 60s；
        * 走 ``_request_with_retry``：复核是可重试的增益操作，网络抖动
          不该让整段窗口变成「未确认」。
        """
        prompt = str(prompt or "")
        if not prompt.strip():
            return ""
        self._require_api_key()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        model = (getattr(self._config, "review_model", "") or self._config.model
                 or self._config.fallback_model)
        budget = int(max_tokens if max_tokens is not None
                     else getattr(self._config, "review_max_tokens", 8192) or 8192)
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, budget),
            "timeout": self._effective_timeout(line_count, REVIEW_TIMEOUT_MAX),
        }
        logger.debug("复核请求 model=%s 共%d行（prompt %d 字符）",
                     model, line_count, len(prompt))
        return self._request_with_retry(params, [0])

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
        # FEATURE(enable-thinking) 2026-10-25：思维链开关（设置里「允许思维链」）。
        # 实测（SiliconFlow）：Qwen3-8B 默认输出 85 reasoning tokens / 4.4s，
        # 置 False 后 0 reasoning / 0.5s——推理链吃掉约 87% 输出预算，是 TPM
        # 限流雪崩的主要放大源，故关掉它同时缓解 429。
        # 只作用于**翻译**请求（用户明确要求）；复核走 review_text，不在此处。
        # 注意：True 时**不发**该参数，保持与历史请求逐字节一致（旧配置行为不变）。
        #
        # FEATURE(fallback-thinking) 2026-10-25：主模型与回退模型各有独立开关。
        # 按**本次实际使用的模型**取对应字段——否则会出现「主模型关了思维链，
        # 切到回退模型又偷偷开起来」的意外。仅当真的切了模型才用回退开关，
        # 因为 use_fallback_model=True 但未配 fallback_model 时实际仍走主模型
        # （见 protocol.build_request_params）。
        if use_fallback_model and getattr(self._config, "fallback_model", ""):
            thinking_on = getattr(self._config, "fallback_enable_thinking", True)
        else:
            thinking_on = getattr(self._config, "enable_thinking", True)
        if not thinking_on:
            params["extra_body"] = {"enable_thinking": False}
        # BUGFIX(timeout-amplify)：按本批行数放大超时，覆盖整块生成耗时。
        params["timeout"] = self._effective_timeout(len(numbered_lines))
        sources = {}
        for item in numbered_lines:
            if len(item) == 3:
                uid, _role, text = item
            else:
                uid, text = item[0], item[1]
            sources[uid] = text
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
    def _effective_timeout(self, line_count: int,
                           cap: float = TIMEOUT_MAX) -> float:
        """按本次批量行数放大超时（BUGFIX(timeout-amplify)）。

        `config.timeout` 是「10 行基准块」的口径；块越大，单次生成越久。
        向上取整到 TIMEOUT_REF_LINES 的整数倍，并夹在 [base, cap]。
        `timeout <= 0` 视为「不限」，直接给硬上限。

        ``cap`` 由调用方给：翻译用 ``TIMEOUT_MAX``，全文复核用更宽的
        ``REVIEW_TIMEOUT_MAX``（复核窗口可达 2000 行）。
        """
        base = float(self._config.timeout)
        if base <= 0:
            return cap
        lines = max(0, int(line_count))
        batches = (lines + TIMEOUT_REF_LINES - 1) // TIMEOUT_REF_LINES
        return min(cap, max(base, base * batches))

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
        model = params.get("model", "?")
        rows = len(ids)
        for attempt in range(1, attempts + 1):
            try:
                if self._on_progress:
                    self._on_progress(f"正在请求模型 {model}（{rows} 行，第 {attempt} 次尝试）……")
                logger.debug(
                    "请求 model=%s uid=%s..%s 共%d行（第%d次）",
                    params.get("model"), ids[0], ids[-1], len(ids), attempt,
                )
                response = self._client.chat.completions.create(**params)
                content = self._extract_content(response)
                if self._on_progress:
                    self._on_progress(f"已收到 {rows} 行响应")
                logger.debug("响应预览：%s", self._preview(content))
                return content
            except Exception as exc:  # noqa: BLE001 - 第三方异常类型不可枚举
                if not self._is_retryable(exc):
                    # §4.3：非可重试错误（含鉴权/参数错误）立即上抛
                    raise ProviderError(f"调用失败（不可重试）：{type(exc).__name__}") from exc
                last_error = exc
                if attempt >= attempts:
                    break
                delay = self._retry_delay(exc, attempt)
                if self._on_progress:
                    self._on_progress(f"调用失败（{type(exc).__name__}），{delay:.0f}s 后重试（{attempt}/{attempts}）……")
                logger.warning(
                    "调用失败（%s），%.0fs 后重试（%d/%d）", type(exc).__name__, delay, attempt, attempts
                )
                self._sleep(delay)
        raise ProviderError(
            f"重试 {attempts} 次仍失败：{type(last_error).__name__ if last_error else 'unknown'}"
        )

    @staticmethod
    def _retry_delay(exc: BaseException, attempt: int) -> float:
        """按错误类型决定退避秒数（BUGFIX 429-storm）。

        优先级：
        1. 服务端 ``Retry-After``（秒数或 HTTP-date）——有就照办，最准；
        2. 限流（429 / RateLimit）→ 长退避（20s、40s、80s…，上限 120s），
           因为 TPM 窗口的恢复尺度是几十秒，短退避必然再次撞限流；
        3. 其它可重试错误（超时/连接/5xx）→ 沿用 §4.3 的 2s、4s、8s。
        """
        hinted = OpenAIProvider._parse_retry_after(exc)
        if hinted is not None:
            return min(hinted, RATE_LIMIT_BACKOFF_MAX)

        status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
        name = type(exc).__name__.lower()
        code = str(getattr(exc, "code", "") or getattr(exc, "type", "") or "").lower()
        is_rate_limit = (
            status == 429
            or "ratelimit" in name
            or any(k in code for k in ("rate_limit", "ratelimit", "50602"))
        )
        if is_rate_limit:
            delay = RATE_LIMIT_BACKOFF_BASE * (BACKOFF_FACTOR ** (attempt - 1))
            return min(delay, RATE_LIMIT_BACKOFF_MAX)
        return BACKOFF_BASE * (BACKOFF_FACTOR ** (attempt - 1))

    @staticmethod
    def _parse_retry_after(exc: BaseException) -> float | None:
        """从异常的响应头读 ``Retry-After``；读不到返回 None（§12：不记头内容）。"""
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # noqa: BLE001 - 头对象可能是任意 Mapping 实现
            return None
        if not raw:
            return None
        text = str(raw).strip()
        try:
            value = float(text)
        except ValueError:
            # HTTP-date 形式：解析成绝对时间再折算
            try:
                from email.utils import parsedate_to_datetime
                import datetime as _dt
                when = parsedate_to_datetime(text)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_dt.timezone.utc)
                value = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
            except Exception:  # noqa: BLE001 - 非法日期当作没有提示
                return None
        return max(0.0, value)

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
