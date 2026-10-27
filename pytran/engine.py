"""翻译编排引擎（W3）。设计文档 §4.3、§5.3、§5.4、ADR-0003。

职责边界（不碰 UI、不读全局配置）：
- 切块（Segmenter）→ 并发调用 Provider → 逐行校验（Validator）→ 失败队列重译；
- 轮次状态机：round_budget 无条件终止线、STUCK/CYCLE 状态哈希、三档策略升级；
- §5.4 申报路由：缺失/申报恢复、单批闸（provider 负责）+ 累计闸（引擎负责）；
- 每次状态变更经 ProjectStore 原子写 project.json；事件经 on_event 回调外发（§9）。

DECISION(§5.3)：轮数上限 5 写死为模块常量 ROUND_BUDGET_CAP，config.round_budget_cap
不得超此值（取 min 兜底）；编号层面的缺失/申报恢复由引擎从 Provider 返回的纯 dict 重建
（合同契约：Provider 丢弃 ParseResult 元数据，只回传 dict[int, str|KeepDeclared]）。

DECISION(§5.4)：申报标记仅在重试轮（round_no > 1）注入；累计闸由引擎按
keep_declare_min_total / keep_declare_rate_limit 拥有并判定，单批闸由 provider 负责。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from pytran.config import Config, TermProfile
from pytran.interfaces import (
    Detection, EventCallback, KeepDeclared, ProtocolError, Provider, Uid,
    Validator,
)
from pytran.models import Project, Segment, SegmentStatus
from pytran.project import JsonProjectStore
from pytran.segmenter import Segmenter

logger = logging.getLogger(__name__)

# §5.3 代码硬上限：轮数上限 5 写死为常量；config.round_budget_cap 不得超此值。
ROUND_BUDGET_CAP = 5

# §5.3 STUCK 三档策略升级（正常 → 详细原因 → 备用模型 → 强制失败）。
STUCK_STAGES = 3

# FEATURE(fallback-after-failures)：切到回退模型后，保证它至少能跑这么多轮。
# 否则「主模型磨掉大半轮次预算 → 回退只拿到 1 次」的老问题依旧。
FALLBACK_MIN_ROUNDS = 2

# 失败原因码（写入 Segment.error，供 §6.5 人工通道回放）。
REASON_ROUND_BUDGET = "round_budget"
REASON_MODEL_FIXED_POINT = "model_fixed_point"
REASON_KEEP_DECLATE_RATE = "keep_declare_rate_exceeded"

# FEATURE(status-hint) 2026-10-26：块级调用失败必须在底部状态栏可见。
# 用户实测：429（或超时/协议不可解析）时服务器没有正确回复，界面只在进度条上
# 走一格，随后整块行静默变红，看不出发生了什么。
# 原因：_translate_round 用 logger.exception 吞掉 ProviderError/ProtocolError，
# 而本工程所有模块只取 logging.getLogger(__name__)，没有任何 handler，
# 因此 logger.warning/exception 永远不会出现在 UI 上。
# 对策：把异常归类成一句人话，经 on_event("log", {"level": "warn"}) 外发。
# 注意：**只加提示**，不改行级 reason（仍为 "missing"），以免扰动
# _state_key 的哈希（uid:target:error），进而影响 STUCK 判定与回退切换。
_HINT_MAX_CAUSE_CHARS = 120


def _failure_hint(exc: BaseException) -> str:
    """把块级调用异常归类成一句用户能看懂的原因。

    既看异常类型名，也看异常链（provider 会把 SDK 异常包成 ProviderError，
    原始 ``RateLimitError``/``APITimeoutError`` 只留在 ``__cause__`` 和消息里），
    还看 ``status_code`` 属性。
    """
    names: list[str] = []
    texts: list[str] = []
    codes: list[int] = []
    cur: Optional[BaseException] = exc
    hops = 0
    while cur is not None and hops < 5:
        names.append(type(cur).__name__)
        texts.append(str(cur))
        code = getattr(cur, "status_code", None)
        if isinstance(code, int):
            codes.append(code)
        cur = cur.__cause__ or cur.__context__
        hops += 1
    joined = " ".join(names + texts).lower()

    if isinstance(exc, ProtocolError) or "protocolerror" in joined:
        return "服务器回复无法解析（协议错误）"
    if 429 in codes or any(
        k in joined
        for k in ("ratelimit", "rate_limit", "429", "50602", "tpm limit")
    ):
        return "请求被限流（429 / TPM 超限），服务器未正常回复"
    if 401 in codes or 403 in codes or "authentication" in joined:
        return "API Key 无效或无权限（401/403），服务器拒绝回复"
    if any(k in joined for k in ("timeout", "timed out", "readtimeout")):
        return "请求超时，服务器未在规定时间内回复"
    if any(k in joined for k in ("connection", "connect error", "network", "dns")):
        return "网络连接失败，服务器未回复"
    if any(500 <= c <= 599 for c in codes) or "internalserver" in joined:
        return "服务器内部错误（5xx），未返回有效回复"
    detail = (texts[0] if texts else "").strip().replace("\n", " ")
    if len(detail) > _HINT_MAX_CAUSE_CHARS:
        detail = detail[:_HINT_MAX_CAUSE_CHARS] + "…"
    suffix = f"：{detail}" if detail else ""
    return f"调用失败（{type(exc).__name__}）{suffix}"


class TranslationEngine:
    """翻译编排引擎，实现 interfaces.Engine（§4.3、§5.3、§5.4、ADR-0003）。

    线程模型：内部用 ThreadPoolExecutor 并发调用 provider，但所有对 project 的
    写回都在主线程顺序完成（先 collect 各 future，再逐个 result 应用），因此引擎
    本身不持锁、不碰 UI；事件只通过 on_event 回调外发。
    """

    def __init__(
        self,
        provider: Provider,
        validator: Validator,
        store: Optional[JsonProjectStore] = None,
    ) -> None:
        self.provider = provider
        self.validator = validator
        self.store = store or JsonProjectStore()
        # 协作式中断标志（§9 停止按钮）。**不在 run() 入口重置**——
        # 否则会吞掉 run() 之前设置的 stop 请求（问题清单 N4/A7）。
        #
        # BUGFIX(stop-flag-sticky) 2026-10-24：标志的生命周期收敛为
        # 「随本轮运行一起结束」——run() 退出时（正常/异常/停止）复位，
        # 保证一次 stop() 不会污染后续运行。用户实测：超时失败后点
        # 「重试失败项」，日志立刻「翻译已被用户停止」，一行都没翻。
        # 原因正是 stop() 置 True 后没有任何出口去复位它，而 UI 复用
        # 同一个 engine 实例（_ensure_engine_ready）。
        # 若 stop() 发生在**没有 run() 在跑**的时刻，就没有出口可复位，
        # 故另提供 reset_stop()，由 UI 在启动新一轮前显式调用。
        self._stop_requested = False
        # 运行序号：旧一轮的收尾不得清掉新一轮刚收到的 stop 请求（跨线程竞态）。
        self._run_seq = 0

    # -- 主入口 ---------------------------------------------------------------
    def run(
        self,
        project: Project,
        config: Config,
        profile: TermProfile,
        on_event: Optional[EventCallback] = None,
        only_uids: Optional[set[Uid]] = None,
    ) -> Project:
        """翻译主入口（BUGFIX(stop-flag-sticky)）。

        停止标志的复位放在 `finally`：无论正常结束、抛异常还是被停止，
        本轮退出时都把它清掉，下次 run() 从干净状态开始。
        `_run_seq` 守卫用于「旧一轮的 finally 晚于新一轮开始」的情形——
        此时不能把新一轮刚设置的 stop 请求清掉。
        """
        self._run_seq += 1
        my_seq = self._run_seq
        try:
            return self._run_inner(project, config, profile, on_event, only_uids)
        finally:
            if self._run_seq == my_seq:
                self._stop_requested = False

    def reset_stop(self) -> None:
        """清掉遗留的停止请求（BUGFIX(stop-flag-sticky)）。

        run() 的 `finally` 只能覆盖「停止发生在运行期间」的情况；若用户在
        两轮之间点了停止（或上一轮在 UI 侧已放弃），标志会悬着。UI 在
        启动新一轮之前调用本方法，确保「重试失败项」不会被上一轮的停止
        请求误杀。
        """
        self._stop_requested = False

    def _run_inner(
        self,
        project: Project,
        config: Config,
        profile: TermProfile,
        on_event: Optional[EventCallback] = None,
        only_uids: Optional[set[Uid]] = None,
    ) -> Project:
        on_event: EventCallback = on_event or (lambda _name, _data: None)
        # §5.3：config 不得超硬上限，取 min 兜底。
        cap = min(int(config.round_budget_cap), ROUND_BUDGET_CAP)

        seg_map = {seg.uid: seg for seg in project.segments}
        # 初始队列：可译行（PENDING/FAILED，§5.3 可重入），受 only_uids 限制。
        # W5g §2：KEEP_DECLARED / KEEP_ORIGINAL 不进队列（见 _build_queue）。
        queue: set[Uid] = self._build_queue(project, only_uids)

        if not queue:
            on_event("log", {"message": "no translatable segments; nothing to do"})
            return project

        segmenter = Segmenter(
            max_lines=max(1, int(getattr(config, "block_lines", 30))),
            context_lines=max(0, int(getattr(config, "context_lines", 3))))
        # 注意：**不**在这里重置 _stop_requested（见 run()：复位由 run() 的
        # finally 负责；入口复位会吞掉 run() 之前设置的 stop 请求，N4/A7）。
        round_no = 0
        history: list[str] = []
        prev_key: Optional[str] = None
        stuck_count = 0
        use_fallback_model = False
        include_errors = False  # §5.3 第1次定点升级标志（合同暂无通道，记录+日志，见交付说明）

        # FEATURE(fallback-after-failures) 2026-10-25：主模型连续失败 N 次后切回退。
        # 背景：既有 STUCK 三档要连续 3 轮状态哈希相同才升到「切备用模型」档，
        # 而 round_budget_cap 只有 5，实测回退模型只拿到 **1 次**尝试就撞轮数
        # 上限被判死——「有回退但形同虚设」。
        # 本开关按失败**轮次**直接切（默认 0 = 不启用，行为与旧版一致）。
        # 回退后给足轮次：轮数上限动态放宽，保证回退模型至少能跑
        # FALLBACK_MIN_ROUNDS 轮，不被 cap 提前截断。
        fallback_after = max(0, int(getattr(config, "fallback_after_failures", 0) or 0))
        consecutive_failures = 0   # 主模型连续「整轮零进展」计数
        fallback_switched_round = 0

        # §5.4 累计闸计数器（引擎负责；单批闸由 provider 负责）
        declared_cumulative = 0
        processed_cumulative = 0

        on_event("progress", {"started": True, "queue_size": len(queue)})

        while queue:
            if self._stop_requested:
                # N4：停止时给出明确终态（已处理/剩余），让用户能区分
                # 「已排停，正在等当前块跑完」与「按钮没反应」。
                on_event("log", {"message": "翻译已被用户停止"})
                on_event("translate_stopped", {
                    "processed": processed_cumulative,
                    "remaining": len(queue),
                    "round": round_no,
                })
                break
            round_no += 1
            # (1) 轮数硬上限：无条件终止线，先于一切检测判定（§5.3）
            # FEATURE(fallback-after-failures)：切换到回退模型后，把上限放宽到
            # 保证回退模型至少跑 FALLBACK_MIN_ROUNDS 轮，避免「刚切过去就撞线」。
            effective_cap = cap
            if fallback_switched_round:
                effective_cap = max(cap, fallback_switched_round + FALLBACK_MIN_ROUNDS)
            if round_no > effective_cap:
                # FEATURE(status-hint)：终止原因也要在底部状态栏留痕，
                # 否则用户只看到一片红行、不知是预算用尽还是服务器一直没回复。
                on_event("log", {
                    "message": (
                        f"轮次预算用尽（{effective_cap} 轮），仍有 {len(queue)} 行未译出；"
                        "具体失败原因见上面的块级提示"
                    ),
                    "level": "warn",
                    "round": round_no,
                })
                self._force_fail(seg_map, queue, REASON_ROUND_BUDGET, on_event)
                self._atomic_save(project, config)
                break

            on_event("round", {
                "round": round_no,
                "queue_size": len(queue),
                "use_fallback_model": use_fallback_model,
            })

            sent, declared_uids = self._translate_round(
                project, seg_map, config, profile, queue, segmenter,
                allow_keep_declare=(round_no > 1),  # §5.4 申报仅注入重试轮
                use_fallback_model=use_fallback_model,
                on_event=on_event,
            )
            processed_cumulative += sent
            declared_cumulative += len(declared_uids)

            # §5.4 累计闸：样本足够且申报率超阈值 → 本轮申报行降级重译
            if (processed_cumulative >= config.keep_declare_min_total
                    and declared_cumulative / max(1, processed_cumulative) > config.keep_declare_rate_limit):
                for uid in declared_uids:
                    self._fail(seg_map, uid, REASON_KEEP_DECLATE_RATE, attempted_target=None)
                on_event("log", {
                    "message": "keep_declare cumulative rate exceeded; degrading declared rows",
                    "declared": declared_cumulative, "processed": processed_cumulative,
                })

            # 重新计算队列：仍为 FAILED 的行继续重译（§5.3：只重发失败行）
            prev_queue_size = len(queue)
            queue = {uid for uid in queue if seg_map[uid].status == SegmentStatus.FAILED}

            if not queue:
                # (2) 队列为空 → 收敛，完成
                on_event("progress", {"done": True, "round": round_no})
                self._atomic_save(project, config)
                break

            # FEATURE(fallback-after-failures)：按「连续失败轮次」直接切回退模型。
            # 判定放在 STUCK 之前：三档要连续 3 轮哈希相同才切备用模型，太慢；
            # 用户要求「多次失败就跳」，故这里先判。
            # 「本轮无进展」= 队列没缩小（一行都没救回来）。
            if not use_fallback_model and fallback_after > 0:
                if len(queue) >= prev_queue_size:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                if consecutive_failures >= fallback_after:
                    if config.fallback_model:
                        use_fallback_model = True
                        fallback_switched_round = round_no
                        # 关键：主模型阶段累积的 STUCK 计数必须清零。
                        # 否则（如 fallback_after=3 时 stuck_count 已达 2）
                        # 回退模型第 1 轮就凑满 STUCK_STAGES 被判死，
                        # 「保证回退至少跑 N 轮」会落空。
                        stuck_count = 0
                        # 次数填太大时，主模型已磨掉大部分轮次预算，
                        # 回退模型可能只剩 1 轮（实测 fallback_after=4 时正是如此）。
                        # 不强行限制用户输入，但明确警告，避免用户以为配大了更保险。
                        remaining = cap - round_no
                        if remaining < FALLBACK_MIN_ROUNDS:
                            on_event("log", {
                                "message": (
                                    f"提示：回退模型只剩 {max(0, remaining)} 轮预算"
                                    f"（轮数上限 {cap}）。失败次数设得越大，"
                                    "留给回退模型的轮次越少，建议改为 1~2。"
                                ),
                                "round": round_no,
                            })
                        on_event("log", {
                            "message": (
                                f"主模型连续 {consecutive_failures} 轮无进展，"
                                f"切换到回退模型 {config.fallback_model}"
                            ),
                            "round": round_no,
                        })
                        logger.info(
                            "switch to fallback model after %d failed rounds (round %d)",
                            consecutive_failures, round_no)
                    else:
                        # 配了次数但没配回退模型 → 明确告警，避免用户以为生效了
                        on_event("log", {
                            "message": (
                                f"主模型连续 {consecutive_failures} 轮无进展，"
                                "但未配置回退模型（设置 → 回退模型），无法切换"
                            ),
                            "round": round_no,
                        })
                        logger.warning("fallback_after_failures set but no fallback_model configured")
                        consecutive_failures = 0  # 不再重复告警

            # (3)-(5) 状态哈希判定
            state_key = self._state_key(seg_map, queue)

            if state_key == prev_key:
                # (4) STUCK：与上一轮相同 → 三档策略升级
                stuck_count += 1
                logger.info("STUCK stage %d detected (round %d)", stuck_count, round_no)
                on_event("log", {"message": f"STUCK stage {stuck_count}", "round": round_no})
                if stuck_count >= STUCK_STAGES:
                    self._force_fail(seg_map, queue, REASON_MODEL_FIXED_POINT, on_event)
                    self._atomic_save(project, config)
                    break
                if stuck_count == 1:
                    include_errors = True  # 第1次定点：重发并逐行附具体错误原因
                elif stuck_count == 2:
                    # FEATURE(fallback-after-failures)：用户填 0 = 彻底禁用回退，
                    # 连这一档的「切备用模型」也不做（用户要求：0 就是完全不回退）。
                    if fallback_after == 0:
                        logger.info(
                            "fallback disabled (fallback_after_failures=0); "
                            "STUCK stage 2 will NOT switch model")
                    else:
                        use_fallback_model = bool(config.fallback_model)  # 第2次定点：切换备用模型
                        if not config.fallback_model:
                            # DECISION(§5.3)：无 fallback_model 时略升温（provider 默认温度，此处仅记录）
                            logger.info("no fallback_model configured; STUCK stage 2 raises temperature (noop at provider)")
            elif state_key in history:
                # (5) CYCLE：与任意历史轮重复 → 直接收敛到 failed
                logger.info("CYCLE detected (round %d); converging", round_no)
                on_event("log", {"message": "CYCLE detected", "round": round_no})
                self._force_fail(seg_map, queue, REASON_MODEL_FIXED_POINT, on_event)
                self._atomic_save(project, config)
                break
            else:
                # (3) 状态变化 → 继续下一轮，只重发失败行
                stuck_count = 0
                prev_key = state_key

            history.append(state_key)
            self._atomic_save(project, config)

        return project

    def stop(self) -> None:
        """协作式中断：请求停止翻译（当前 in-flight 的块会完成，后续块跳过）。"""
        self._stop_requested = True

    # -- 单轮翻译 -------------------------------------------------------------
    def _translate_round(
        self,
        project: Project,
        seg_map: dict[Uid, Segment],
        config: Config,
        profile: TermProfile,
        queue: set[Uid],
        segmenter: Segmenter,
        *,
        allow_keep_declare: bool,
        use_fallback_model: bool,
        on_event: EventCallback,
    ) -> tuple[int, list[Uid]]:
        """并发翻译当前队列，返回 (发送行数, 本轮申报 uid 列表)。"""
        chunks = segmenter.chunk(project.segments)

        # FEATURE(progress-hint)：把进度回调传给 provider，让 GUI 在
        # 请求/退避期间能看到状态（用户实测：思维链模型 178s 无反馈）。
        if on_event is not None:
            def _provider_progress(msg: str) -> None:
                on_event("log", {"message": msg})
            self.provider._on_progress = _provider_progress
        # 仅发送队列内的行；context 取自完整段序（§4.1），provider 接受 (src,dst) 2-元组
        tasks: list[tuple[list[tuple[Uid, str]], list[tuple[str, str]]]] = []
        for chunk in chunks:
            lines = [(uid, src) for (uid, src) in chunk.lines() if uid in queue]
            if lines:
                tasks.append((lines, list(chunk.context)))

        sent = 0
        declared_uids: list[Uid] = []
        # FEATURE(status-hint)：按「原因」去重收集本轮的块级失败。
        # 一个 2000 行的文件按 block_lines=20 切是 100 块；若服务器全程 429，
        # 每块都往状态栏写一条会把其它信息挤掉（日志面板虽有行数上限，
        # 但刷屏本身就让用户看不到重点）。故同一原因只在首次出现时写详情，
        # 轮末再补一条汇总。
        failed_chunks: dict[str, list[tuple[int, int, int, int]]] = {}
        if not tasks:
            return 0, declared_uids

        # 并发：每 chunk 一次 translate_batch；结果在主线程顺序应用（无共享写竞争）
        with ThreadPoolExecutor(max_workers=max(1, int(config.max_workers))) as ex:
            futures = []
            for lines, context in tasks:
                fut = ex.submit(
                    self.provider.translate_batch,
                    lines, profile, context,
                    allow_keep_declare, use_fallback_model,
                )
                futures.append((fut, lines))
                sent += len(lines)
            done_count = 0
            for fut, lines in futures:
                if self._stop_requested:
                    on_event("log", {"message": "翻译已被用户停止（剩余块跳过）"})
                    break
                done_count += 1
                try:
                    result = fut.result()
                except Exception as exc:  # 网络层耗尽 / 协议不可解析（ProviderError/ProtocolError）
                    logger.exception("translate_batch raised; marking requested rows missing")
                    result = {}
                    # FEATURE(status-hint)：块级失败必须在底部状态栏可见，
                    # 否则用户只看到「进度走一格 + 一批行变红」，不知是 429/超时/协议错。
                    first_uid, last_uid = lines[0][0], lines[-1][0]
                    hint = _failure_hint(exc)
                    seen = failed_chunks.setdefault(hint, [])
                    seen.append((done_count, first_uid, last_uid, len(lines)))
                    if len(seen) == 1:  # 同一原因只详述一次，避免刷屏
                        on_event("log", {
                            "message": (
                                f"第 {done_count}/{len(futures)} 块失败：{hint}"
                                f"（行 {first_uid}..{last_uid} 共 {len(lines)} 行已记为失败）"
                            ),
                            "level": "warn",
                            "reason": hint,
                            "uid_first": first_uid,
                            "uid_last": last_uid,
                        })
                on_event("chunk_done",
                         {"done": done_count, "total": len(futures),
                          "rows": len(lines)})
                declared_uids.extend(
                    self._apply_result(
                        seg_map, profile, lines, result, allow_keep_declare, on_event,
                    )
                )
        # FEATURE(status-hint)：本轮多块失败时补一条汇总。
        # 只有 1 块失败时不重复——上面那条详情已经把原因说清楚了。
        total_chunks = sum(len(hits) for hits in failed_chunks.values())
        if total_chunks > 1:
            parts = []
            total_rows = 0
            for hint, hits in failed_chunks.items():
                parts.append(f"{hint}（{len(hits)} 块）")
                total_rows += sum(h[3] for h in hits)
            on_event("log", {
                "message": (
                    f"本轮共 {total_chunks} 个块（{total_rows} 行）未拿到有效回复："
                    + "；".join(parts)
                ),
                "level": "warn",
                "reason": "chunk_failures_summary",
            })
        return sent, declared_uids

    # -- 结果应用 -------------------------------------------------------------
    def _apply_result(
        self,
        seg_map: dict[Uid, Segment],
        profile: TermProfile,
        lines: list[tuple[Uid, str]],
        result: dict[Uid, "str | KeepDeclared"],
        allow_keep_declare: bool,
        on_event: EventCallback,
    ) -> list[Uid]:
        """把一批翻译结果写回 Segment；返回本轮申报的 uid 列表。"""
        requested = {uid for (uid, _src) in lines}
        returned = set(result.keys())
        declared_uids: list[Uid] = []

        # 缺失：请求了却未回传 → FAILED（原因标注 uid）
        for uid in (requested - returned):
            self._fail(seg_map, uid, f"missing: uid={uid}", attempted_target=None)
            on_event("segment", {"uid": uid, "status": "failed", "reason": "missing"})

        for uid, val in result.items():
            seg = seg_map.get(uid)
            if seg is None:
                continue  # 合同外 uid，忽略
            if isinstance(val, KeepDeclared):
                # §5.4 申报路由：置 keep_declared，保留原文进入 §6.5 人工通道
                seg.status = SegmentStatus.KEEP_DECLARED
                seg.target = seg.source
                seg.error = val.reason
                seg.attempts += 1
                declared_uids.append(uid)
                on_event("segment", {
                    "uid": uid, "status": "keep_declared", "reason": val.reason,
                })
                continue

            target = val
            detection, repaired = self.validator.check(
                seg.source, target, profile, allow_keep_declare=allow_keep_declare,
            )
            if detection == Detection.PASS.value:
                seg.status = SegmentStatus.TRANSLATED
                seg.target = repaired if repaired is not None else target
                seg.error = None
                on_event("segment", {"uid": uid, "status": "translated"})
            else:
                # 失败：记录末次尝试译文供回放，状态置 FAILED
                self._fail(seg_map, uid, detection, attempted_target=target)
                on_event("segment", {
                    "uid": uid, "status": "failed", "reason": detection,
                })
        return declared_uids

    # -- 工具 -----------------------------------------------------------------
    @staticmethod
    def _build_queue(project: Project, only_uids: Optional[set[Uid]]) -> set[Uid]:
        """W5g §2 根因修复：重译队列只收 PENDING / FAILED。

        KEEP_DECLARED（AI 申报保留）与 KEEP_ORIGINAL（人工保留原文）此前被
        错误地纳入队列，导致「已被接受/已声明保留」的段在「全量翻译」、
        「失败重试」、「试译」三处都会被自动重发覆盖。现在二者均不进队列——
        与 ``ui._translatable_uids`` 同一口径。SKIPPED 本就不进（§14）。
        """
        queue: set[Uid] = set()
        for seg in project.segments:
            if seg.status in (SegmentStatus.PENDING, SegmentStatus.FAILED):
                if only_uids is None or seg.uid in only_uids:
                    queue.add(seg.uid)
        return queue

    @staticmethod
    def _fail(
        seg_map: dict[Uid, Segment],
        uid: Uid,
        reason: str,
        attempted_target: Optional[str],
    ) -> None:
        seg = seg_map.get(uid)
        if seg is None:
            return
        seg.status = SegmentStatus.FAILED
        if attempted_target is not None:
            seg.target = attempted_target
        seg.error = reason
        seg.attempts += 1

    @staticmethod
    def _force_fail(
        seg_map: dict[Uid, Segment],
        queue: set[Uid],
        reason: str,
        on_event: EventCallback,
    ) -> None:
        for uid in queue:
            TranslationEngine._fail(seg_map, uid, reason, attempted_target=None)
            on_event("segment", {"uid": uid, "status": "failed", "reason": reason})

    @staticmethod
    def _state_key(seg_map: dict[Uid, Segment], queue: set[Uid]) -> str:
        """队列状态哈希：失败行集合 + 各行译文 + 错误原因（不含 attempts，避免自扰动）。"""
        parts = []
        for uid in sorted(queue):
            seg = seg_map.get(uid)
            if seg is None:
                continue
            parts.append(f"{uid}:{seg.target!r}:{seg.error!r}")
        return "|".join(parts)

    def _atomic_save(self, project: Project, config: Config) -> None:
        # §3.2 / §5.3：每次状态变更原子写 project.json（经 JsonProjectStore）
        self.store.save(project, Path(config.work_dir))


# 保留一个干净的导出名，便于测试与未来壳层引用。
Engine = TranslationEngine
