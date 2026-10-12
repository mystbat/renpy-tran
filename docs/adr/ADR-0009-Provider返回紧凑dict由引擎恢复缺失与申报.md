# ADR-0009：Provider 返回紧凑 dict，缺失/申报由引擎恢复

- 日期：2026-10-09
- 状态：已接受
- 影响范围：`interfaces.Provider.translate_batch` 契约（§4.2/§6.5）、W3 engine

## 背景

`interfaces.py` 把 `Provider.translate_batch` 的返回类型定为
`TranslationResult = dict[int, str | KeepDeclared]`——即 Provider 只回「uid → 译文/KeepDeclared」，
**丢弃了 §4.2 三层解析产出的 `ParseResult` 元数据**（行级 success/keep_declared/reject/downgraded
等明细、累计闸告警等）。

这与 W2 的实现存在张力：W2 的 `parse_response` 产出的是富结构 `ParseResult`，包含逐行状态与
`KeepDeclareAccumulator` 联动。但契约层（T0 冻结的 `interfaces.py`）明确只要求紧凑 dict。

## 决策

确认契约层以紧凑 dict 为唯一对外形态：

1. **Provider 只负责「请求的行 → 译文/KeepDeclared」映射**；行级失败（缺行、拒绝）一律通过
   **字典缺键**或 **`KeepDeclared` 占位**表达，不回传任何解析元数据。
2. **引擎（W3）是唯一恢复点**，负责把紧凑 dict 还原为完整状态机所需信息：
   - `missing`：请求键集合 − 返回键集合 → 这些 uid 置 `FAILED(reason="missing: uid=…")`；
   - `declared`：返回值为 `KeepDeclared` 的 uid → 置 `KEEP_DECLARED`（error=原因、target=source）；
   - 其余：译文交给 `validators.check` 做四态判定。
3. **W2 的 `ParseResult` 降级/累计闸告警只服务于 Provider 内部**决策（决定是否降级整批、是否进
   降级模式）；对外只需把最终判定（降级行当 UNTRANSLATED 重译、申报行当 `KeepDeclared`）落到
   返回的 dict 上即可。累计计数器由引擎持有并回传，但**不要求** Provider 把计数器状态写进返回值。

## 理由

- 契约简单性：UI/引擎只需关心「每行最终是什么」，逐行解析细节是 Provider 内部实现；
- 引擎是唯一能做跨轮状态机的地方（缺失恢复、定点升级、轮数硬上限都依赖引擎层的全局视图），
  把「dict → Segment 状态」的还原放在引擎是单一职责；
- 避免让 `TranslationResult` 承载两套语义（既是协议映射又是解析元数据），降低跨模块耦合；
- `KeepDeclared` 本身已携带 `reason`，足以向引擎表达「该行被申报保留」且原因可读，无需额外字段。

## 后果

- W3 engine 必须实现 `requested - returned` 的缺失恢复与 `KeepDeclared` 分发（已落地于
  `TranslationEngine._apply_result`）；
- Provider 实现方（W2 `OpenAIProvider` 或其他）不得假定引擎会读取 `ParseResult`；若需上报
  解析级告警（如累计闸触发），应通过 `on_event("log", …)` 或降级模式标志，而非返回值；
- 契约文件 `interfaces.py` 维持 `dict[int, str|KeepDeclared]` 不变（T0 冻结）；本 ADR 是对该
  契约实现意图的官方解释，不构成契约修改；
- 集成阶段需确保：Provider 漏回 uid 时引擎不静默吞掉（必须 `missing` 恢复），否则会漏译。
