# BUGFIX(timeout-amplify + stop-flag-sticky)：翻译必然超时 + 停止标志跨轮粘连

- 日期：2026-10-24
- 触发：用户实测反馈（两个独立现象，同一轮测试中发现）
- 状态：✅ 已修复（**461 全量用例通过**，新增 5 用例）
- 提交：`2a5ce02`（与 W5i 角色名、backfill 修正一并合入 master）

---

## Bug 1：清空工程重新导入后，翻译必然超时

### 1.1 现象

用户报告（原话）：

> 在之前的几次修改前，翻译还能正常运行。这次测试完全清空了工程，
> 重新生成了 `script.rpy` 并导入，然后点击翻译之后。进度条一直滑，
> 调用失败（`APITimeoutError`），2s 后重试（1/2）

两个可观察特征：

1. **进度条一直滑**——不是百分比推进，是不确定态（indeterminate）；
2. **一次失败耗时约 180s**，然后才打印「2s 后重试（1/2）」。

### 1.2 定位

排除服务端故障（同类请求手工可通）后，锁定**两个叠加的代码缺陷**：

**缺陷 A：超时被 SDK 层放大 3 倍**

`OpenAIProvider._build_client` 构造 `openai.OpenAI(...)` 时未指定
`max_retries`，SDK 默认 `max_retries=2`。而本项目 `_request_with_retry`
自己已有一层 §4.3 重试状态机，两层叠加后：

```
一次逻辑尝试 = (SDK 1 + 2 次重试) × timeout = 3 × 60s ≈ 180s
```

实测 `APITimeoutError` 出现在 **181.9s**，与 3×60s 吻合。

**缺陷 B：60s 固定超时对满块本就不够**

`config.ini` 的 `timeout = 60.0`、`block_lines = 50`。实测同一 50 行块：

| 模型 | 50 行耗时 |
|---|---|
| `Qwen/Qwen2.5-7B-Instruct` | 32.9 ~ 47.1s |
| `Qwen/Qwen3-8B`（开思考） | 177.8s |
| `Qwen/Qwen3-8B`（`enable_thinking=False`） | 39.5s |
| `deepseek-ai/DeepSeek-R1-0528-Qwen3-8B` | 89.5s（n=20） |

即：即使不放大，60s 对推理模型也是**必然超时**。用户「之前的几次修改前
还能正常运行」是因为当时用的模型/块大小不同——这是**配置与代码耦合的
脆弱点**，不是偶发。

**为什么进度条一直滑**：`round` 事件触发 `_set_translate_busy(True)`，
把进度条设为 `setRange(0, 0)`（不确定态）；只有 `chunk_done` 才
`_set_translate_busy(False)` 并显示百分比。请求卡在超时重试里时，
永远收不到 `chunk_done`，进度条就一直在滑——这是**症状**，不是原因。

### 1.3 修复

`pytran/providers.py`：

1. **关掉 SDK 层重试**，重试只由 §4.3 单层状态机负责：

   ```python
   kwargs = {"api_key": ..., "timeout": ..., "max_retries": 0}
   ```

2. **超时随批量行数放大**，新增 `_effective_timeout(line_count)`：

   ```python
   TIMEOUT_REF_LINES = 10   # 基准行数：≤10 行用 config.timeout 原值
   TIMEOUT_MAX = 900.0      # 硬上限
   ```

   规则：`base × ceil(lines / 10)`，夹在 `[base, TIMEOUT_MAX]`；
   `timeout <= 0` 视为「不限」，直接给硬上限。

3. `translate_batch` 在 `build_request_params` 之后覆盖 `params["timeout"]`。

`block_lines=50` 时实际超时 = **300s**（5×60s），覆盖实测最慢的 274.8s。

### 1.4 为什么不只是「把 timeout 调大」

调大 `config.ini` 只能救当前配置，下次有人把 `block_lines` 提到 100
或换个推理模型又会复发。放大规则写进代码后，**块越大超时越长**这个
约束由代码保证，不再依赖人工同步两个配置项。

---

## Bug 2：点过「停止」后，「重试失败项」一行都不翻

### 2.1 现象

用户报告（原话）：

> 现在点击"重试失败项"：重试 206 个失败项…… 翻译已被用户停止
> 为什么会自动停止？

即：点了「重试失败项」，日志立刻打「重试 206 个失败项……」，紧接着
就是「翻译已被用户停止」，**一个请求都没发出**。

### 2.2 定位

`TranslationEngine._stop_requested` 是协作式中断标志：

- `stop()` 置 `True`；
- `run()` 的 while 循环开头检查它，为 `True` 就 break 并打
  「翻译已被用户停止」。

问题在于**没有任何地方把它复位回 `False`**。而 UI 的
`_ensure_engine_ready` 会**复用同一个 engine 实例**
（`if self.deps.engine is not None: return`）。于是：

```
用户点「停止」→ stop() → _stop_requested = True（这一轮正常结束）
...后续任何一次「重试失败项」/「开始翻译」...
→ run() 第一次循环检查 → True → 立刻 break
```

**一次 stop 污染此后所有运行**，直到重启程序。

### 2.3 修复

标志生命周期收敛为「**随本轮运行一起结束**」，分两处配合：

**`pytran/engine.py`**

1. `run()` 拆为 `run()` + `_run_inner()`：`run()` 只负责生命周期，
   `finally` 里复位标志；
2. `_run_seq` 序号守卫：旧一轮的 `finally` 若晚于新一轮开始，不得清掉
   新一轮刚收到的 stop 请求（跨线程竞态）；
3. 新增 `reset_stop()` 公开方法。

```python
def run(self, ...):
    self._run_seq += 1
    my_seq = self._run_seq
    try:
        return self._run_inner(...)
    finally:
        if self._run_seq == my_seq:
            self._stop_requested = False
```

**`pytran/ui.py`**

`_translate_in_background` 在 `_ensure_engine_ready()` 之后、
`worker()` 启动之前调用 `engine.reset_stop()`。

### 2.4 设计取舍（审核重点）

**为什么不在 `run()` 入口复位？**
入口复位会吞掉「用户在点『开始』到 worker 真正启动之间点停止」的请求，
整轮翻译照跑（问题清单 N4/A7）。这是已记录的既有约束，不能回退。

**为什么 `finally` 复位还不够？**
`stop()` 可能在**没有 `run()` 在跑**的时刻被调用（上一轮已放弃、
UI 已切页），此时没有出口去清它。所以必须由 UI 侧显式
`reset_stop()` 兜底。两者是互补关系，不是重复：

| 场景 | 谁来清 |
|---|---|
| 运行期间被停止 → 本轮退出 | `run()` 的 `finally` |
| 两轮之间点了停止（无 run 在跑） | UI 的 `reset_stop()` |

**为什么需要 `_run_seq`？**
`stop()` 从主线程调、`run()` 在 worker 线程跑。若旧一轮的 `finally`
执行时新一轮已经 `stop()` 过，无守卫的复位会把新请求吃掉。

---

## 3. 测试

### 3.1 新增用例（5 个）

`tests/test_protocol.py`（Bug 1）：

| 用例 | 断言 |
|---|---|
| `test_build_client_disables_sdk_level_retries` | 构造 SDK 时 `max_retries == 0` |
| `test_effective_timeout_scales_with_line_count` | 0/1/10 行 → 60s；11 行 → 120s；50 行 → 300s；10000 行 → 硬上限 |
| `test_translate_batch_timeout_covers_whole_block` | 50 行块实际请求 timeout == 300s |
| `test_single_logical_attempt_bounded_by_one_timeout` | `max_retry_rounds=1` 时只发 1 次请求、无退避 |

`tests/test_engine.py`（Bug 2）：

| 用例 | 断言 |
|---|---|
| `test_stop_flag_does_not_leak_into_next_run` | 第一轮 stop 后，第二轮正常翻完并产出译文 |
| `test_stop_flag_cleared_even_when_provider_raises` | provider 抛异常时标志也被清（`finally` 生效） |

`tests/test_ui_translate_page.py`（Bug 2，端到端）：

| 用例 | 断言 |
|---|---|
| `test_retry_failed_works_after_a_previous_stop` | `engine.stop()` → `_retry_failed()` → provider 确实收到调用、标志复位、进度标签无「已停止」 |

### 3.2 回归结果

```
461 passed, 0 failed（--junitxml 核对：tests=461 failures=0 errors=0）
```

基线为修复前的 456 passed，净增 5。

---

## 4. 影响面

| 维度 | 影响 |
|---|---|
| 提示词 / 协议 | 无变更（Bug 1 只改 timeout 与 SDK 参数） |
| 解析 / 护栏 / 写回 | 无变更 |
| 引擎重试状态机 | 层级变清晰：SDK 不再插手，§4.3 是唯一重试源 |
| 停止语义 | 从「全局粘性」改为「单轮有效」，行为更符合直觉 |
| 历史工程 | 无需迁移；受影响批次重翻即可 |

**需要用户注意的行为变化**：单次请求最长等待从 60s 变为最长 900s。
进度条在等待期间仍是不确定态（滑动态）——这是既有设计，本次未改；
若希望长等待时给出可感知反馈，需另开任务。

---

## 5. 审核清单

- [ ] `providers.py` 的 `max_retries=0` 是否会影响其它依赖 SDK 重试的调用路径
      —— 目前只有 `translate_batch` 与 `complete_text` 用同一 client，
      前者走 §4.3 重试、后者明确不重试，均不依赖 SDK 层。
- [ ] `_effective_timeout` 的 300s（50 行）是否符合预期成本；
      硬上限 900s 是否需要下调。
- [ ] `run()`/`_run_inner()` 拆分后，是否有外部代码直接调用过
      `_run_inner`（应为内部方法）。
- [ ] `reset_stop()` 由 UI 调用这一约定，是否需要写进接口契约文档。
- [ ] 2.4 节的三条设计取舍是否认可（尤其「入口不复位」不能改）。
