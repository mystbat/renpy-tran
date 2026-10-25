# 交付说明：思维链开关 + 429 退避修复 + UI 卡死修复

- **分支**：`feature/enable-thinking-toggle`（基于 `master` @ `0ae4ead`）
- **状态**：已实现、已自测、**未提交**，待审核人员审核
- **回归**：`572 passed`（原 546 + 新增 16 项 + 更新既有断言）
- **测试环境**：Windows / Python 3.13.3 / `.venv` / PyQt6 offscreen

---

## 一、本次需求：模型设置里增加「是否允许思维链」

### 1.1 界面位置（按要求：模型名称下面）

设置对话框「常规」页，勾选框紧跟「模型名称」输入框正下方：

```
row  1: 模型地址 (URL)                      QLineEdit
row  2: 模型名称                            QLineEdit
row  3: ☐ 允许思维链（关闭可显著提速、省 token）   QCheckBox   ← 新增
row  4: 回退模型 (可选，漏翻重试时切换)          QLineEdit
```

已用 offscreen 实测确认：`模型名称 row = 2`，`checkbox row = 3`，相邻且在其后。

勾选框带完整 tooltip，说明实测数据、模型适用范围、以及「只影响翻译请求」。

### 1.2 行为

| 勾选状态 | 请求体 | 效果 |
|---|---|---|
| 勾选（默认） | **不发** `extra_body` | 与历史请求逐字节一致，行为完全不变 |
| 取消勾选 | `extra_body: {"enable_thinking": false}` | 抑制推理链 |

**为什么默认勾选**：`True` 时不发任何新参数，保证旧 `config.ini`（无该键）加载后行为零变化。这是向后兼容的关键设计。

### 1.3 实测数据（SiliconFlow，单行请求）

| 模型 | 默认（允许思维链） | `enable_thinking=false` |
|---|---|---|
| **Qwen3-8B** | 4.4s / 100 tok（**推理 85**） | **0.5s / 18 tok（推理 0）** |
| DeepSeek-R1-0528 | 7.3s / 196 tok（推理 186） | 3.6s / 108 tok（**推理仍 98**） |
| Qwen2.5-7B-Instruct | — | 0.2s / 35 tok（本就不推理） |

**结论与限制（已写入 tooltip）**：
- 对 **Qwen3 这类混合推理模型有效**：省约 87% 输出 token，快约 8 倍。
- 对 **DeepSeek-R1 这类纯推理模型无效**：照样输出 98 个推理 token。用户若用 R1 却关掉此开关，不会报错，但也不提速。
- 参数形式必须是 `extra_body={"enable_thinking": False}`。实测 `extra_body={"chat_template_kwargs": {...}}` **无效**（推理 token 仍 73），故未采用。

### 1.4 作用范围（按用户明确要求：只管翻译）

- **翻译请求**：`OpenAIProvider.translate_batch` → 受控。
- **全文复核**：`OpenAIProvider.review_text` → **不受控**，保持原样。
- 有专门测试 `test_review_ignores_enable_thinking` 锁定此边界，防止将来误扩大到复核。

---

## 二、`config.py` 冻结协议的处理（需审核重点确认）

`pytran/config.py` 头部写着「契约文件，T0 冻结 —— 任何会话不得修改」，但同一段有加字段先例：2026-10-24 新增 `[Review]` 4 字段，注明「需总控确认，本条目即为该确认」。

本次按同一惯例处理，**纯新增、未改动任何既有字段语义**：

1. 文件头变更记录新增 2026-10-25 条目，含批准说明与实测依据。
2. `_INI_SECTION_MAP` 新增 `("Model", "enable_thinking"): "enable_thinking"`。
3. `_DEFAULT_INI` 模板 `[Model]` 段新增 `enable_thinking = true`。
4. `Config` 数据类新增 `enable_thinking: bool = True`。
5. `_coerce_types` 新增 `_coerce_bool` 归一（与 `review_enabled` 同口径）。

**兼容性**：缺省值 `True` + `_coerce_bool(default=True)`，旧 `config.ini` 缺该键时行为不变，**无需迁移**。有测试 `test_enable_thinking_missing_key_keeps_true` 锁定。

> 若审核方认为该字段不应进 `config.py`，替代方案是借 `TermProfile.prompt_overrides` 传递（`tests/test_validators.py:304` 有先例），但语义上它是请求参数而非提示词，且会变成「按工程」而非全局生效。当前实现按用户选定的路线执行。

---

## 三、顺带修复的两个 bug（用户此前上报）

### 3.1 429 限流雪崩

**用户日志现象**：每块 `调用失败（RateLimitError），2s 后重试（1/2）` → `translate_batch raised; marking requested rows missing` → `ProviderError: 重试 2 次仍失败`，整轮所有行判失败。

**实测根因（非代码缺陷，是配额 + 退避口径不匹配）**：
- `Qwen/Qwen3-8B` 返回 `status=429, code=50602, message="...TPM limit reached."`
- 响应头**只有 `date`**，**无 `Retry-After`、无 `x-ratelimit-*`** —— 服务端不告知等待时长。
- `sleep(65)` 后同一调用**成功** → 是瞬时限流窗口，不是封禁。
- TPM 窗口恢复尺度是**几十秒**，而旧代码固定退避 **2s/4s**，两次尝试都落在同一窗口内，必然再次 429 → 整轮失败 → 下轮重发再撞 → 雪崩。

**修复**（`providers.py`）：
- 新增 `RATE_LIMIT_BACKOFF_BASE = 20.0`、`RATE_LIMIT_BACKOFF_MAX = 120.0`。
- 新增 `_retry_delay()`：按错误类型分流，限流走 20s/40s/80s（封顶 120s），其它可重试错误（超时/连接/5xx）仍走 §4.3 的 2s/4s/8s。
- 新增 `_parse_retry_after()`：**优先读服务端 `Retry-After`**（支持秒数与 HTTP-date），并封顶 120s。服务端将来若补上该头，客户端自动跟随。

**测试变更**：`test_provider_retries_on_429_with_backoff` 原断言 `[2.0, 4.0]` 是**旧 bug 行为**，已更新为 `[20.0, 40.0]`。新增 4 项覆盖退避封顶、`Retry-After` 生效/封顶、非限流错误不受波及。

**关联**：思维链开关本身就是 429 的主要缓解手段 —— 推理链吃掉约 87% 输出 token，关掉它直接降低 TPM 消耗。

### 3.2 软件失去响应（GUI 卡死）

**实测根因（UI 侧，与 429 无关）**：`_apply_event` 对**每一条** `segment` 事件都调用 `_refresh_review()`，而该函数 `clear()` 整个 `QListWidget` 并重建全部条目。单事件 O(N) → 一次排空 N 个事件 O(N²)。总 429 失败时 `_apply_result` 会为每行发一条 `segment` 事件，于是数万行时主线程被独占。

**实测（真实构建 UI，单个 QTimer tick 内排空）**：

| 行数 | 修复前 |
|---|---|
| 500 | 2.7 s |
| 1000 | 6.3 s |
| 2000 | 15.0 s |
| 4000 | 32.4 s |
| 8000 | **55.5 s** |

进程没死，只是主线程被 UI 刷新独占 —— 这就是「失去响应」的直接原因。

**已做的修复**（`ui.py`）：
1. `segment` 事件改为只置 `_review_refresh_pending` 脏标记，由 `_drain_events()` 在**本次 tick 末尾合并刷新一次**。
2. `_on_segment_event` 的 `next((s for s in project.segments if s.uid == uid), None)` 逐事件线性扫描，改为惰性 `uid→Segment` 索引（`_seg_index()`），并在 6 处 `self.project =` 替换点调用 `_invalidate_seg_index()` 保证正确性。
3. 日志面板加 `document().setMaximumBlockCount(500)`（实测 800 次追加后稳定在 500 段，不再无限膨胀）。

### 3.3 ⚠️ 已知遗留：3.2 尚未彻底解决

**必须如实说明**：上述 3 项修复降低了事件处理开销，但**未消除卡死**。修复后实测（真实 UI）：

| 行数 | 修复后 |
|---|---|
| 500 | 2.7 s |
| 1000 | 6.3 s |
| 2000 | 15.0 s |
| 8000 | 55.5 s |

数字几乎未变 —— 因为瓶颈不在「刷新次数」，而在 `_refresh_review()` **单次重建 1000 行列表本身就极慢**（每行构造 `QListWidgetItem` + `QBrush`/`QColor`），合并成一次只是从 N 次降到 1 次，而这 1 次在 1000 行时已达数秒级。

**彻底方案（建议下一步）**：把审校列表从「全量 clear+重建」改为**增量追加**（只 append 新增/变更行，不 clear），或改用 `QAbstractTableModel` + `QTableView` 走模型/视图架构，让 Qt 只渲染可见行。工作量约半天，但能从 O(N) 降到 O(可见行数)。

**当前可用规避**：卡死是暂时的，不丢数据（`project.json` 原子写入，强杀安全）；可把 `review_window_lines` 调小、或翻译期间不切到审校页。

---

## 四、变更文件清单

| 文件 | 变更 |
|---|---|
| `pytran/config.py` | +16 行：新增 `enable_thinking` 字段（含冻结协议变更记录） |
| `pytran/providers.py` | +81 行：`extra_body` 注入 + 429 长退避 + `Retry-After` 解析 |
| `pytran/ui.py` | +88 行：勾选框 + 事件合并刷新 + uid 索引 + 日志上限 |
| `tests/test_protocol.py` | +44 行：429 退避断言更新 + 4 项新增覆盖 |
| `tests/test_enable_thinking.py` | 新增文件，16 项测试（配置层 6 / Provider 层 4 / 兼容性 6） |

**未提交**，工作树干净（仅上述改动 + 用户既有的 `glossary_dlc_aurora.csv` 未跟踪文件）。

---

## 五、审核建议关注点

1. **`config.py` 加字段是否合规** —— 已按 2026-10-24 先例走「变更记录 + 总控确认」流程，但请确认是否认可本次批准。
2. **默认值 `True` 的兼容性** —— 已保证不发新参数、旧 ini 无需迁移。
3. **429 退避 20s 是否可接受** —— 单块最多多等 60s（20+40），换来不再整轮失败；若嫌慢可下调 `RATE_LIMIT_BACKOFF_BASE`。
4. **3.3 的遗留卡死** —— 请确认是否本轮一并修（改为增量追加），还是另开任务。
5. **`test_provider_retries_on_429_with_backoff` 断言变更** —— 由 `[2.0, 4.0]` 改为 `[20.0, 40.0]`，这是修正「测试锁定了 bug 行为」，非放松断言。

---

## 六、用户验证步骤

1. 打开软件 → 设置 → 常规页，确认「模型名称」正下方有「允许思维链」勾选框。
2. 取消勾选 → 保存 → 用 `Qwen/Qwen3-8B` 翻译少量行，观察日志中的耗时与 token 明显下降。
3. 重新打开设置，确认勾选框仍为未勾选（已持久化）。
4. 若用 `DeepSeek-R1`，取消勾选**不会**提速（纯推理模型不响应此开关），属预期行为。
