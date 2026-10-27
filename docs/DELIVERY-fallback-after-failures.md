# 交付说明：主模型多次失败后自动切换回退模型

- **分支**：`feature/enable-thinking-toggle`（基于 `master` @ `0ae4ead`）
- **状态**：已实现、已自测、**未提交**，待审核人员审核
- **回归**：`588 passed`（原 546 + 思维链 16 + 回退 16 + 既有断言更新）
- **测试环境**：Windows / Python 3.13.3 / `.venv` / PyQt6 offscreen

---

## 一、先回答提问：现在的设计会不会切到回退模型？

**会切，但实测下来几乎等于没有。** 我没有只读代码下结论，而是用模拟 provider
跑了真实引擎（`block_lines=100`、`round_budget_cap=5`、provider 每批恒抛异常），
记录每次调用的 `use_fallback_model`：

| `fallback_after_failures` | 实际调用轨迹（False=主模型，True=回退） | 回退拿到几轮 |
|---|---|---|
| **0（改前等效）** | `[F, F, F, T]` | **1 轮** |
| 1 | `[F, T, T, T]` | 3 轮 |
| 2 | `[F, F, T, T]` | 2 轮 |
| 3 | `[F, F, F, T, T]` | 2 轮 |
| 4 | `[F, F, F, T]` | **1 轮** |

改造前的三条硬事实：

1. **回退模型只拿到 1 次尝试。** 既有切换点固定在 STUCK 三档的第 2 档
   （`engine.py` `elif stuck_count == 2`），此时 `round_no` 已到第 4 轮，
   第 5 轮立刻撞上 `round_no > cap`（5）无条件终止线 → `_force_fail` 全部行
   → `model_fixed_point`。回退模型没有第二次机会。
2. **失败次数设得越大，效果越差。** 上表最后一行即是证据：填 4 时主模型磨掉
   4 轮才切，切完只剩 1 轮。用户凭直觉"调大更保险"会踩坑。
3. **`max_retry_rounds = 1` 时回退永远触发不了。** STUCK 要连续 3 轮状态哈希
   相同才升到第 2 档，轮数不够根本走不到那一档。

一个**澄清**（与最初担心相反，实测排除）：失败原因每轮变化（429 / 超时 /
协议错轮转）**不影响**切换。因为 provider 抛异常时该行 `error` 保持
`missing: uid=n` 不变，`_state_key` 仍稳定，STUCK 照样能推进。所以
"错误文本扰动导致永不切换"这个假设不成立，真正的问题是轮次预算被主模型磨光。

---

## 二、本次改动

### 2.1 新增设置项

设置对话框「常规」页，紧跟「回退模型」正下方：

```
row 4: 回退模型 (可选，漏翻重试时切换)        QLineEdit
row 5: 失败几次后切回退模型 (0=不回退)        QLineEdit   ← 新增
```

已 offscreen 实测：`回退模型 row=4`、`新字段 row=5`，相邻且在其后；
默认值渲染为 `0`（不是空白），保存后写入 `config.ini` 的 `[Engine]` 段，
重开对话框回显 `2`。

### 2.2 语义

| 取值 | 行为 |
|---|---|
| `0`（默认） | **完全不回退**，连既有 STUCK 第 2 档的「切备用模型」也一并禁用 |
| `1` | 主模型第 1 轮无进展后，第 2 轮即切回退 |
| `N` | 主模型连续 N 轮「队列没缩小」后切回退 |
| 配了 N 但 `fallback_model` 为空 | 不崩、不空转，日志明确告警「无法切换」 |
| 旧 config.ini 无此键 | 读作 `0`，行为与改造前一致（除 2.2 第 1 行的差异，见 §4） |

**「无进展」的定义**：本轮结束后，待重译队列没有比上一轮缩小（一行都没救回来）。
不用"异常次数"，因为一行失败但另一行成功时整体仍有进展，不该触发切换。

### 2.3 保证回退模型有足够轮次（`FALLBACK_MIN_ROUNDS = 2`）

切换后把轮数上限动态放宽到 `max(cap, 切换轮次 + 2)`，避免"刚切过去就撞线"。
同时**清零 `stuck_count`**——否则主模型阶段累积的 STUCK 计数（如填 3 时已到 2）
会让回退模型第 1 轮就凑满 `STUCK_STAGES` 被判死，这个"保证轮次"就落空了。

填得过大导致回退轮次不足时，日志会给出具体提示：

> 提示：回退模型只剩 1 轮预算（轮数上限 5）。失败次数设得越大，
> 留给回退模型的轮次越少，建议改为 1~2。

按你的决定，**不强行限制可填上限，只警告**。

### 2.4 `config.py` 契约变更（新增 1 字段，未改动既有语义）

按 2026-10-24 `[Review]` 4 字段的先例走"追加字段 + changelog"流程：

- `_DEFAULT_INI` 增 `[Engine] fallback_after_failures = 0`
- `_INI_SECTION_MAP` 增 `("Engine", "fallback_after_failures")`
- `Config` dataclass 增 `fallback_after_failures: int = 0`
- `_coerce_types()` 的 int 列表增该字段
- 文件头 changelog 增 2026-10-25 条目，写明背景与"仅新增、未改动既有字段语义"

**审核关注点**：`config.py` 头部声明"T0 冻结，任何会话不得修改"。本次是
**第三次**追加字段（前两次：`[Review]` 4 字段、`enable_thinking`）。
若审核认为冻结就该真冻结，替代方案是放进 `TermProfile.prompt_overrides`
（`tests/test_validators.py:304` 有先例），但那样无法在设置页做类型化输入框，
也没有默认值兜底。此处按你此前"走 config.py 新字段"的决定执行。

---

## 三、改动文件

| 文件 | 改动 |
|---|---|
| `pytran/config.py` | +1 字段（changelog / `_DEFAULT_INI` / `_INI_SECTION_MAP` / dataclass / `_coerce_types`） |
| `pytran/engine.py` | 新增 `FALLBACK_MIN_ROUNDS`；`_run_inner` 增连续失败计数与切换判定、动态放宽轮数上限、切换时清零 `stuck_count`、次数过大告警；STUCK 第 2 档在 `fallback_after==0` 时不再切模型 |
| `pytran/ui.py` | 设置页 `fields` 增 1 项；`_NUMERIC_ATTRS` 增该字段；`_coerce_setting` 增 int 分支（**单独处理，允许 0**）；修复默认值渲染 `0 or ""` → 空白的问题 |
| `tests/test_fallback_after_failures.py` | **新增** 16 项 |
| `tests/test_ui_settings_dialog.py` | 字段计数 17→18，下标随之 +1（4 处） |
| `tests/test_ui_translate_page.py` | 字段计数 17→18，`block_lines` 下标 13→14 |

---

## 四、行为差异声明（审核必看）

**`fallback_after_failures = 0` 与改造前不完全等价。** 改造前，即使用户没配
任何"失败次数"概念，STUCK 第 2 档仍会切备用模型（上表第一行 `[F,F,F,T]`）。
现在填 `0` 表示**彻底不切**，实测轨迹为 `[F,F,F,F]`。

这是按你的明确要求做的（"0 时不回退"）。但它意味着：

- 旧 ini 升级后，若用户原本依赖 STUCK 第 2 档的自动切换，现在不会发生了。
- 若希望保留旧行为，把该字段设为 `3`（STUCK 三档的等效触发轮次）即可近似。

**如果审核认为"默认值应保持旧行为"，把 `_DEFAULT_INI` 与 dataclass 的默认值
从 `0` 改成 `3` 即可，其余代码无需改动。**

---

## 五、测试

```
588 passed in 38.73s
```

新增 16 项覆盖：默认 0 完全不回退、按次数切换、切换后不退回主模型、
比 STUCK 更快、回退至少 2 轮、回退轮次多于旧的 1 轮、未配回退模型不崩、
全部 failed 不静默成功、UI 层 0 不被钳成 1、负数钳到 0、留空/非法保留原值、
ini 往返、旧 ini 缺键默认 0、设置页默认显示 `"0"` 而非空白。

两个测试曾报 `FileNotFoundError`（`test_migrate_persists_project_json`、
`test_review_fields_present_and_defaulted`），单独重跑均通过——是另一会话
在同工作区并发跑 pytest 时删掉了共享的 `.pytest_tmp` 目录所致，与本次改动无关。

---

## 六、审核建议关注

1. **§四的行为差异**：`0` 是否该"连 STUCK 也禁用"，默认值该是 `0` 还是 `3`。
2. **`_coerce_setting` 的 0 特例**：其它数值字段都是 `max(1, int(raw))`，
   这个字段必须允许 `0`，代码里单独开了分支。若后续有人"统一钳位"会破坏语义，
   已用 `test_ui_coerce_keeps_zero` 锁住。
3. **`stuck_count` 清零**：不清零会让"保证回退 2 轮"失效，属易漏点。
4. **`getattr(...) or ""` 的渲染坑**：`0` 是合法值时会被渲染成空白，
   已在 `ui.py` 修正并用 `test_settings_field_shows_zero_not_blank` 锁定。
5. **`config.py` 第三次追加字段**：见 §2.4 末段。

---

## 七、用户验证步骤

1. 设置 → 常规 → 「回退模型」下方填「失败几次后切回退模型」，例如 `2`，保存。
2. 确认 `config.ini` 的 `[Engine]` 段出现 `fallback_after_failures = 2`。
3. 正常翻译；若主模型连续失败，日志会出现：
   `主模型连续 2 轮无进展，切换到回退模型 <模型名>`。
4. 想彻底关闭回退，填 `0`。
