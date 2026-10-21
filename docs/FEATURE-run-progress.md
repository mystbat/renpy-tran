# BUGFIX(busy-stuck) + FEATURE(run-progress)：停止后进度条动画残留修复 + 本次运行细进度条

- 日期：2026-10-21
- 触发：用户实测反馈（① 翻译停止后按钮已灰但进度条仍在流动动画；
  ② 建议在主进度条下增加一条较细的本次运行进度）
- 状态：✅ 已修复/已实现（385 全量用例通过，新增 5 用例）

---

## 1. BUGFIX(busy-stuck)：停止后流动动画残留

### 现象

翻译停止后：停止按钮已灰、其他按钮已亮（`translate_stopped` 事件已
正确处理按钮态），但进度条仍处于 indeterminate 流动动画。

### 根因

`round` 事件把进度条切为 indeterminate（`setRange(0, 0)`），恢复正常
依赖后续 `chunk_done` / `progress` 事件中的 `_set_translate_busy(False)`。
但终态事件 `translate_finished` / `translate_stopped` 的处理器**都没有
恢复模式**——当事件序列以 round 收尾（或 round 与终态事件之间的
chunk_done/progress 因竞态未及时消费）时，indeterminate 永久残留。

### 修复

两个终态事件处理器（`translate_finished` / `translate_stopped`）显式：

- `_set_translate_busy(False)`——恢复确定性模式；
- `_run_progress_reset()`——细条填满后隐藏（见下）。

防御式收口：无论事件序列如何交叠，终态必然回到静止状态。

## 2. FEATURE(run-progress)：本次运行细进度条

主进度条下新增一条 **6px 细进度条**，语义与主条正交：

| 条 | 语义 | 显示时机 |
|---|---|---|
| 主条 | 工程**累计**完成度（done/total，含历史已译与人工审校） | 常驻 |
| 细条 | **本次运行**已发/待发行数（round 事件的 queue_size 为分母） | 仅翻译运行时 |

生命周期：

- `round` 事件 → 细条清零、显示，分母 = 本轮队列行数（每次开跑重置，
  不复用上次残留值）；
- `chunk_done` → 按本块行数推进（`rows` / 分母）；
- 终态（finished/stopped）→ 填满 100% 后隐藏（给「跑完了」的视觉确认）。

顺带修正：round 事件文案读取 `queue_size`（引擎实际字段名；此前写的
`queue` 恒为空，界面显示「待译 ? 行」）。

## 3. 测试（tests/test_ui_progress.py，5 用例）

- `test_stop_resets_busy_mode`：round → stopped，主条恢复确定性、
  细条隐藏（BUGFIX 主回归守卫）；
- `test_finish_resets_busy_mode`：finished 终态同样恢复；
- `test_run_progress_lifecycle`：显示→25%→50%→隐藏；
- `test_run_progress_second_run_resets`：再次开跑清零；
- `test_chunk_done_updates_main_progress`：主条行为回归守卫。

回归：`python -m pytest tests -p no:cacheprovider` → **385 passed**。

## 4. 影响面

- 仅 UI 层事件处理与新增控件，引擎/协议/解析不动。
- `_refresh_progress`（工程级刷新，导入/清空等场景触发）不触碰细条——
  细条只由翻译事件管理，避免「清空工程把运行中的细条藏掉」这类越权。
