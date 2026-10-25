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
    Detection, EventCallback, KeepDeclared, Provider, Uid,
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

# 失败原因码（写入 Segment.error，供 §6.5 人工通道回放）。
REASON_ROUND_BUDGET = "round_budget"
REASON_MODEL_FIXED_POINT = "model_fixed_point"
REASON_KEEP_DECLATE_RATE = "keep_declare_rate_exceeded"


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
            if round_no > cap:
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
            queue = {uid for uid in queue if seg_map[uid].status == SegmentStatus.FAILED}

            if not queue:
                # (2) 队列为空 → 收敛，完成
                on_event("progress", {"done": True, "round": round_no})
                self._atomic_save(project, config)
                break

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
        # 仅发送队列内的行；context 取自完整段序（§4.1），provider 接受 (src,dst) 2-元组
        tasks: list[tuple[list[tuple[Uid, str]], list[tuple[str, str]]]] = []
        for chunk in chunks:
            lines = [(uid, src) for (uid, src) in chunk.lines() if uid in queue]
            if lines:
                tasks.append((lines, list(chunk.context)))

        sent = 0
        declared_uids: list[Uid] = []
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
                except Exception:  # 网络层耗尽 / 协议不可解析（ProviderError/ProtocolError）
                    logger.exception("translate_batch raised; marking requested rows missing")
                    result = {}
                on_event("chunk_done",
                         {"done": done_count, "total": len(futures),
                          "rows": len(lines)})
                declared_uids.extend(
                    self._apply_result(
                        seg_map, profile, lines, result, allow_keep_declare, on_event,
                    )
                )
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
