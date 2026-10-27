# 交付说明：429 等「服务器未正确回复」在底部状态栏提示

- **分支**：`feature/enable-thinking-toggle`（基于 `master` @ `0ae4ead`）
- **状态**：已实现、已自测、**未提交**，待审核人员审核
- **回归**：`628 passed`（基线 609 + 本次新增 19）
- **测试环境**：Windows / Python 3.13.3 / `.venv` / PyQt6 offscreen

---

## 一、问题复现与根因

### 1.1 用户报的现象

> 429 或者其他情况造成服务器端没有正确回复。应该在最下面的状态信息栏里提示。

### 1.2 实测复现（修复前）

用 offscreen Qt 起真实界面、真实引擎，provider 每批恒抛 429
（`round_budget_cap=1`、10 行、`block_lines=100` → 1 块）：

| 观察点 | 修复前实际值 |
|---|---|
| 引擎发出的 `log` 事件数 | **0 条** |
| 10 行的最终状态 | 全部 `failed`，`reason` 均为 `"missing"` |
| 底部状态栏内容 | 始终停在 `就绪。步骤：选择目录 → 导入 → 翻译 → 审校 → 导出写回；API Key 在「⚙ 设置」中填写` |
| 异常去向 | 只在 stderr：`pytran.interfaces.ProviderError: 重试 1 次仍失败：RateLimitError` |

用户看到的就是「进度条走了一格，然后一片行变红」，**没有一个字解释为什么**。

### 1.3 根因（两层，缺一不可）

**第一层：异常被吞掉，没有转成 UI 事件。**

`pytran/engine.py` `_translate_round()` 里每个 chunk 的 `fut.result()` 外面是：

```python
except Exception:
    result = {}          # ← 吞掉，什么都不说
```

然后 `_apply_result()` 按「请求了但没回传」把该块所有行记为
`reason="missing"`。行级 reason 是给状态机用的（`missing` / `validator` /
`round_budget` / `model_fixed_point`），**它本身不是给人看的提示**，
而且界面上只体现为一格红底。

**第二层（关键）：本工程根本没有日志 handler。**

我 grep 了全仓：每个模块都只有 `logger = logging.getLogger(__name__)`，
**没有任何一处** `basicConfig` / `FileHandler` / `addHandler`。所以
`logger.warning(...)` / `logger.exception(...)` 的输出**永远到不了任何界面**——
只在控制台可见，而打包成 GUI 后连控制台都没有。

这两层叠加的结果就是：错误信息在内存里产生过，然后彻底消失。

---

## 二、改动内容

### 2.1 新增 `_failure_hint()`：把异常翻译成人话

`pytran/engine.py`，模块级函数，紧跟在三个 reason 常量之后：

```python
_HINT_MAX_CAUSE_CHARS = 120

def _failure_hint(exc: BaseException) -> str:
    ...
```

它沿 `__cause__` / `__context__` 最多走 5 跳，收集异常链上每一层的
**类型名**、**消息**、**`status_code` 整数值**，然后归类：

| 判定依据 | 输出提示 |
|---|---|
| `isinstance(exc, ProtocolError)` 或链上出现 `protocolerror` | `服务器回复无法解析（协议错误）` |
| 429 / `ratelimit` / `50602` / `tpm limit` | `请求被限流（429 / TPM 超限），服务器未正常回复` |
| 401 / 403 / `authentication` | `API Key 无效或无权限（401/403），服务器拒绝回复` |
| `timeout` / `timed out` / `readtimeout` | `请求超时，服务器未在规定时间内回复` |
| `connection` / `connect error` / `network` / `dns` | `网络连接失败，服务器未回复` |
| 500–599 任一状态码 / `internalserver` | `服务器内部错误（5xx），未返回有效回复` |
| 都不匹配 | `调用失败（ValueError）：boom`（原始消息截断到 120 字 + `…`） |

**为什么按字符串和 `status_code` 双重判定**：`providers.py` 把底层 SDK 异常
包成 `ProviderError(f"重试 N 次仍失败：{type(last_error).__name__}")`，
原始 `RateLimitError` 只以**名字**留在消息里；而 SDK 有时又保留
`status_code` 属性。两条路都覆盖，才不会漏判。

### 2.2 块级失败 → `log` 事件（warn 级）

`_translate_round()` 的 `except` 分支改为：

```python
except Exception as exc:
    logger.exception("translate_batch raised; marking requested rows missing")
    result = {}
    first_uid, last_uid = lines[0][0], lines[-1][0]
    hint = _failure_hint(exc)
    seen = failed_chunks.setdefault(hint, [])
    seen.append((done_count, first_uid, last_uid, len(lines)))
    if len(seen) == 1:                       # 同一原因只详述一次
        on_event("log", {
            "message": (f"第 {done_count}/{len(futures)} 块失败：{hint}"
                        f"（行 {first_uid}..{last_uid} 共 {len(lines)} 行已记为失败）"),
            "level": "warn",
            "reason": hint,
            "uid_first": first_uid,
            "uid_last": last_uid,
        })
```

`level="warn"` 会被 `ui.py` `_log()` 渲染成红字（`#b03030`），
与普通信息区分开。

### 2.3 防刷屏：同因去重 + 轮末汇总

一个 2000 行的文件按 `block_lines=20` 切是 **100 块**。若服务器全程 429，
每块写一条会把状态栏彻底淹没，用户反而看不到重点。

处理方式：

- 同一 `hint` **只在首次出现时写详情**（含块序号与行区间）；
- 本轮结束时，若失败块数 **> 1**，补一条汇总：

  ```
  本轮共 5 个块（10 行）未拿到有效回复：请求被限流（429 / TPM 超限），服务器未正常回复（5 块）
  ```

- 只有 1 块失败时**不补汇总**——详情已经说清楚了，重复反而噪音。

### 2.4 轮次预算用尽也留痕

原来 `round_no > effective_cap` 直接 `_force_fail` 后 `break`，界面上同样
只有「一片红行」。现在补一条：

```
轮次预算用尽（1 轮），仍有 10 行未译出；具体失败原因见上面的块级提示
```

否则用户分不清「服务器一直没回」和「引擎自己把轮次用光了」。

---

## 三、刻意**不**改的地方（契约保护）

这是本次改动最需要审核关注的一点。

### 3.1 行级 `reason` 保持 `"missing"`

`_apply_result()` 的缺失分支**一个字没动**：

```python
self._fail(seg_map, uid, f"missing: uid={uid}", attempted_target=None)
on_event("segment", {"uid": uid, "status": "failed", "reason": "missing"})
```

原因：`TranslationEngine._state_key()` 只哈希 `uid:target:error`。
STUCK 三档判定、CYCLE 判定、以及上一轮交付的
`FEATURE(fallback-after-failures)` 回退切换**全部依赖这个哈希的稳定性**。
如果把「429 / 超时」写进行级 `reason` 或 `seg.error`，哈希就跟着变，
STUCK 可能永远凑不齐 3 次相同、回退永远不切——把一个「只加提示」的
改动变成状态机回归。

所以：**提示只走 `log` 事件，不进状态机。**

### 3.2 不引入新事件名

`tests/test_e2e_mock.py::test_e2e_atomic_save_no_ui` 断言所有事件名属于
`{"progress", "log", "segment", "round", "chunk_done"}`。本次只用 `log`，
集合未变（有专门用例锁定）。

### 3.3 不动 provider 的重试/退避

429 的长退避（`20 * 2**(attempt-1)`，上限 120s）是上一轮交付的内容，
本次不碰。`providers.py` 仍不持有 event callback——提示在引擎层统一产生，
避免同一件事被 provider 和 engine 各报一遍。

---

## 四、验证

### 4.1 探针（真实引擎 + offscreen 真实界面）

```
=== 1. _failure_hint 归类 ===
  429 RateLimit    -> 请求被限流（429 / TPM 超限），服务器未正常回复
  429 status only  -> 请求被限流（429 / TPM 超限），服务器未正常回复
  timeout          -> 请求超时，服务器未在规定时间内回复
  protocol         -> 服务器回复无法解析（协议错误）
  auth             -> API Key 无效或无权限（401/403），服务器拒绝回复
  5xx              -> 服务器内部错误（5xx），未返回有效回复
  unknown          -> 调用失败（ValueError）：boom

=== 2. 引擎事件（429，单块）===
  log 事件数: 2
    | 第 1/1 块失败：请求被限流（429 / TPM 超限），服务器未正常回复（行 0..9 共 10 行已记为失败）
    | 轮次预算用尽（1 轮），仍有 10 行未译出；具体失败原因见上面的块级提示

=== 3b. 多块全 429（block_lines=2 → 5 块）===
  log 事件数: 3（1 条详情 + 1 条汇总 + 1 条预算）
    | 第 1/5 块失败：请求被限流（429 / TPM 超限），服务器未正常回复（行 0..1 共 2 行已记为失败）
    | 本轮共 5 个块（10 行）未拿到有效回复：请求被限流（429 / TPM 超限），服务器未正常回复（5 块）
    | 轮次预算用尽（1 轮），仍有 10 行未译出；具体失败原因见上面的块级提示

=== 4. 事件名集合 ===
  ['chunk_done', 'log', 'progress', 'round', 'segment']   合法: True

=== 5. UI 底部栏（真实 QTextEdit）===
  喂事件后行数: 3
    | 就绪。步骤：选择目录 → 导入 → 翻译 → 审校 → 导出写回；API Key 在「⚙ 设置」中填写
    | 第 1/1 块失败：请求被限流（429 / TPM 超限），服务器未正常回复（行 0..9 共 10 行已记为失败）
    | 轮次预算用尽（1 轮），仍有 10 行未译出；具体失败原因见上面的块级提示
  底部栏出现失败提示: 是
```

对照修复前（`log` 事件 0 条、底部栏不变）——**bug 已消除**。

### 4.2 新增回归 `tests/test_status_hint.py`（19 项）

| 用例 | 锁定的契约 |
|---|---|
| `test_429_failure_surfaces_in_log_event` | 429 必须产生 warn 级 `log` 事件 |
| `test_timeout_failure_surfaces_with_timeout_wording` | 超时要说是「超时」 |
| `test_protocol_error_surfaces_with_parse_wording` | 协议错误要说是「无法解析」 |
| `test_auth_error_surfaces_as_key_problem` | 401/403 指向 API Key |
| `test_message_includes_uid_range_and_row_count` | 提示带行号区间与行数，可定位 |
| `test_same_reason_many_chunks_is_deduplicated` | 5 块全 429 → 只 1 条详情 + 1 条汇总 |
| `test_single_chunk_has_no_redundant_summary` | 单块失败不补汇总 |
| `test_row_level_reason_stays_missing` | 提示语**不得**泄漏进行级 reason / `seg.error` |
| `test_no_new_event_names_introduced` | 不引入新事件名 |
| `test_round_budget_exhaustion_is_also_reported` | 预算用尽也要留痕 |
| `test_failure_hint_classification`（7 参数） | 六类归类的逐项断言 |
| `test_failure_hint_falls_back_to_exception_name` | 无法归类时也说清异常类型 |
| `test_failure_hint_truncates_huge_message` | 超长消息截断，不撑爆状态栏 |

### 4.3 全量回归

```
628 passed
```

基线 609 + 新增 19，**零失败**。既有的
`test_fallback_after_failures.py`(16) / `test_fallback_enable_thinking.py`(21) /
`test_enable_thinking.py`(16) / `test_e2e_mock.py` 全部保持通过。

---

## 五、需要审核人员确认的三点

1. **措辞**。提示文案是我按「用户看得懂」写的（例如
   `请求被限流（429 / TPM 超限），服务器未正常回复`）。若要统一风格或
   加英文，改 `_failure_hint()` 一处即可，测试断言的是关键词
   （`限流` / `超时` / `解析` / `Key` / `5xx`），换措辞需同步改断言。

2. **`reason` 字段的用法**。`log` 事件里我额外带了
   `reason` / `uid_first` / `uid_last` 三个字段，目前 UI 只消费
   `message` 和 `level`。留着是给后续「点提示跳到失败行」用的，
   如果审核认为多余可以删。

3. **是否需要弹窗**。当前只在底部状态栏留痕，不弹 `QMessageBox`——
   429 在长文件翻译中可能反复出现，弹窗会打断。若审核希望
   「整轮全失败」时额外弹一次，属于新需求，需要单独确认。

---

## 六、本次**未**解决的问题（如实声明）

1. **UI 卡死问题只缓解、未根治。** 2000 行时审校页重建仍要
   ~15.0s，瓶颈是 `_refresh_review()` 的 O(N) 逐行重建，
   与本次 429 提示是两件独立的事。上一轮已做过缓解，但根因未修。

2. **429 没有真正的限流等待策略。** 目前是固定指数退避
   （20s → 40s → 80s，上限 120s），不看服务器返回的配额头
   （SiliconFlow 的 429 响应**确实不带** `Retry-After` /
   `x-ratelimit-*`，已实测确认）。所以无法做到「等到额度恢复再发」。

3. **提示不能点击跳转。** 见 §5.2。

---

## 七、改动文件清单

| 文件 | 改动 |
|---|---|
| `pytran/engine.py` | 新增 `_failure_hint()` + `_HINT_MAX_CAUSE_CHARS`；`_translate_round()` 的 `except` 分支发 `log` 事件（去重 + 汇总）；`round_no > effective_cap` 分支补一条提示；import 补 `ProtocolError` |
| `tests/test_status_hint.py` | **新增**，19 项回归 |
| `docs/DELIVERY-status-hint.md` | **新增**，本文 |

`config.py` / `providers.py` / `ui.py` 本次**未改动**（它们的改动属于
前几轮交付，见 `DELIVERY-enable-thinking-and-429-fix.md`、
`DELIVERY-fallback-after-failures.md`、`DELIVERY-fallback-enable-thinking.md`）。
