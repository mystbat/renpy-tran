# W5g 任务书：审校「保留原文」状态 + 重试/写回范围修正

> 任务来源：用户实测反馈（2026-10-21）：
> ① 审校页只有「通过(Ctrl+S)」与「失败(f)」两个操作，缺「保留原文」——
>    AI 申报保留的行与用户想保留原文的行没有正式的终态；
> ② 「即使被标记为通过，依然会被重试」——实测确认，根因见 §2；
> ③ 写回时被保留的段不应写回。

---

## 1. 现状确认（实现者必读，已逐行核实）

### 1.1 重试范围的事实

`engine._build_queue`（engine.py）收集的状态集合：

```python
PENDING, FAILED, KEEP_DECLARED   # ← KEEP_DECLARED 也在队列里
```

**这就是用户问题的根因**：`KEEP_DECLARED`（AI 申报保留/人工裁定保留）的行
**每次全量翻译都会被重试**。语义矛盾：

- KEEP_DECLARED 的定义是「保留原文」；
- 但 `_build_queue` 把它当待译行 → 重发 → 模型对中文（或原文）再处理 → 浪费
  token 且可能产生垃圾译文；
- 用户在审校页把它标成「通过」（REVIEWED）确实能跳过——但那是**误用**：
  REVIEWED 的语义是「人工接受了译文」，而保留行的 target 是原文本身，
  写回时会把原文写回 new 行（内容上无害但语义混乱，且状态欺骗了审校统计）。

### 1.2 审校操作的事实

`ui_state.py` 只有两个操作：
- `accept_current()` → REVIEWED（a / Ctrl+S）
- `flag_failed_current()` → FAILED（f）

没有「保留原文」操作。UI 申报保留行（KEEP_DECLARED）没有从审校页一键
确认的入口。

### 1.3 写回行为

`regenerate` 对有 target 的行替换引号内容，无 target 的行逐字节保留。
KEEP_DECLARED 行 target=source → 写回后 new 行 = 原文（英文）——
**内容上恰好正确**，但依赖隐式行为且状态语义不对。

---

## 2. 需求

### 2.1 新增 SegmentStatus：`KEEP_ORIGINAL`

- 值：`keep_original`；语义：**人工裁定保留原文，永不重试，写回时该段
  保持原文**（与 KEEP_DECLARED 的区别：KEEP_DECLARED 是 AI 申报待裁定，
  KEEP_ORIGINAL 是人工终态）。
- **契约变更**：`models.py SegmentStatus` 与 `ui_state.REVIEW_FILTERS` 加该值
  ——本任务书即为总控确认，实现者可直接改 models.py（与 ADR-0006 前例一致：
  任务书批准即视为契约变更授权），但需在交付说明记录。
- `project.json` 兼容：旧文件无此状态 → 无迁移需求（新状态只在新操作产生）。

### 2.2 审校页新增「保留」操作

- 按钮「保留原文」+ 快捷键（建议 `k`，与列表导航不冲突需验证）；
- 行为：`keep_original_current()` → status=KEEP_ORIGINAL，target=source
  （显式写原文，写回与显示一致），error=None；
- 对 KEEP_DECLARED 行 → 这是「人工裁定：确实保留」的确认动作，
  同时可把残留英文短语并入 `profile.exempt_english`（复用 W5b 逻辑，
  可选勾选，默认并入）；
- 预览面板/列表对该状态用独立颜色（建议灰）与标识 `[保留]`。

### 2.3 重试范围修正（核心）

| 入口 | 现状 | 修改后队列来源 |
|---|---|---|
| 开始自动翻译 | PENDING/FAILED/KEEP_DECLARED | **PENDING/FAILED**（KEEP_DECLARED 与 KEEP_ORIGINAL 均排除——前者已被人工裁定过，排除后由审校页确认） |
| 重试失败项 | FAILED | FAILED（不变） |
| 试译 | PENDING/FAILED/KEEP_DECLARED | PENDING/FAILED |

- **注意**：KEEP_DECLARED 从 `_build_queue` 排除后，AI 申报行的出口收敛为：
  审校页「保留原文」→ KEEP_ORIGINAL（终态），或「通过」（接受译文）/「失败」（重试）。
- `_translatable_uids()`（ui.py）同步改口径，与 `_build_queue` 一致；
- 估算 token、试译取样同步（三处共用 `_translatable_uids` 的话只改一处）。

### 2.4 写回语义

- KEEP_ORIGINAL 行：target=source → regenerate 写回原文 → **正确**（保留段
  本来就该是原文）；无需特殊分支，但 `_refresh_progress` 等统计把
  KEEP_ORIGINAL 计入「完成」而非「失败」；
- 导出 CSV：该行译文列 = 原文（明确可见）。

---

## 3. 测试（pytest，≥10 用例）

| # | 用例 | 断言 |
|---|---|---|
| 1 | keep_original_current 基本行为 | status=KEEP_ORIGINAL, target=source |
| 2 | KEEP_DECLARED 行调用 keep_original_current → 确认语义转换 | status=KEEP_ORIGINAL |
| 3 | _build_queue 排除 KEEP_ORIGINAL / KEEP_DECLARED | 不进队列 |
| 4 | 开始自动翻译不重试 KEEP_ORIGINAL 行 | run 后 status 不变 |
| 5 | 重试失败项不含 KEEP_ORIGINAL | 同上 |
| 6 | 试译取样排除 KEEP_ORIGINAL | 不进试译 |
| 7 | 写回：KEEP_ORIGINAL 行 new=原文 | regenerate 输出含原文 |
| 8 | _translatable_uids 口径与 _build_queue 一致 | 三处集合相等 |
| 9 | 审校过滤器含 keep_original | 列表可见 |
| 10 | project.json 往返保留该状态 | 序列化不丢 |

## 4. 用户实测清单（交付说明必附）

1. AI 申报行 → 审校页「保留原文」→ 该行变灰、不再被重试；
2. 全量翻译跑两轮 → 保留行状态不变、不消耗 token；
3. 写回 → 游戏内该行为原文；
4. CSV 导出 → 该行译文列 = 原文；
5. 重开 GUI → 状态保留。

## 5. 交付

- 分支 `task/W5` 继续；red→green；交付说明含 pytest 输出 + git log + 实测清单；
- 完成后交总控审核（重点：契约变更记录 + _build_queue 三处口径一致性）。
