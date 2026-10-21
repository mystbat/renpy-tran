# W5f 任务书：导入数据安全——重新导入不得静默丢弃已译内容

> 任务来源：用户实测问询（2026-10-21）确认的数据丢失风险。
> 现状：`ui.py _do_import_files` 直接 `build_project()` 替换内存工程并覆盖
> `project.json`——翻译完成后未写回就重新导入，**全部译文静默丢失**。
> 授权：`.worktrees/W5` + `task/W5`；pytest 必须；GUI 实测归用户；禁主树。
> 冻结契约不动；`backfill.py` 的兜底链直接复用。

---

## 1. 现状确认（实现者必读）

```
_do_import_files:
    segs = parse(选中文件)          # 全部 PENDING，不带旧译文
    self.project = build_project()  # ← 全新对象，旧 project 丢弃
    store.save(self.project)        # ← 旧 project.json 被覆盖
```

触发条件：工程有已译内容（`stats.translated > 0` 或 `reviewed > 0`）时
执行任何一次「导入」。

## 2. 需求：导入前置检查 + 三选项

`_do_import_files` 在替换工程**之前**检查：

```python
has_translation = any(s.status in (TRANSLATED, REVIEWED)
                      for s in self.project.segments)
```

- **无已译内容** → 维持现行为（直接导入，无弹窗）；
- **有已译内容** → 弹三选对话框（QMessageBox 自定义按钮）：

| 选项 | 行为 |
|---|---|
| **合并导入**（推荐默认） | 新旧工程按文件名分组对齐；同文件内**按特征码归一化匹配**保留旧译文（复用 `backfill.py` 的 `normalize_feature` + `match_translations`，§6.2 兜底链）；对不上的旧行进「遗留译文」列表供用户在日志区查看；新文件的新行正常 PENDING |
| **清空重导** | 先把当前 `project.json` 复制到 `bak/project-<YYYYmmdd-HHMMSS>.json`（`bak/` 已 gitignore），日志记录丢弃统计（`丢弃 translated X 行 / reviewed Y 行`），然后全新工程 |
| **取消** | 什么都不做，返回 |

- 对话框需展示统计摘要：「当前工程：已译 X 行 / 审校 Y 行。重新导入将影响这些数据。」
- 三个选择都必须可取消（点 X = 取消）。

## 3. 合并导入的实现要点

```
旧工程 project_old（内存中即将被替换的）
新解析 new_segs, new_files

对 new_segs 逐段：
  1. 归一化键 = normalize_feature(new.feature) + normalize(source 前缀可选)
     —— 优先 (文件名 + 特征码归一化) 匹配旧段
  2. 匹配到且旧段 status ∈ (TRANSLATED, REVIEWED) → 新段继承
     target / status=REVIEWED（保守：继承的译文视为已审校，防止自动重译覆盖）
  3. 匹配到但旧段是 PENDING/FAILED → 新段保持 PENDING（旧译文本可丢）
  4. 匹配不到 → 新段保持 PENDING
files 条目：同文件合并 sha256 更新；仅旧工程有的文件 → 保留条目并记日志
game_root：以新选择为准
term_profile：**保留当前 profile.json**（术语表不随导入重置——
  与 a57d0db/7676ac9 的「清空工程」语义区分：那是用户显式清空）
```

- 复用 `backfill.normalize_feature`（引号/空白归一化），**不要**重写匹配逻辑；
- 合并冲突（同键多旧段）→ 后值覆盖 + 日志告警，与 §6.2 语义一致；
- `state = UiState(new_project)` 重建照旧；
- 「清空工程」按钮（`7676ac9` 已有）的行为对齐：也先备份 project.json 到
  `bak/`，与合并路径共用 `_backup_current_project()` 辅助方法。

## 4. 测试（pytest，≥8 用例）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 无已译内容导入 → 无弹窗直接导入 | 行为与现状一致 |
| 2 | 合并导入：同特征码匹配 → target 继承、status=REVIEWED | 旧译文不丢 |
| 3 | 合并导入：新文件新增行 → PENDING | 新行可译 |
| 4 | 合并导入：特征码对不上 → 旧译文进日志遗留列表 | 不静默丢弃 |
| 5 | 清空重导：project.json 备份到 bak/ 且内容可回读 | 备份有效 |
| 6 | 取消 → 工程与 project.json 均不变 | 幂等安全 |
| 7 | term_profile 保留：合并后 profile.terms 不变 | 术语表不受导入影响 |
| 8 | mock：`_confirm_import_mode` 返回值驱动的三分支 | 弹窗逻辑可测 |

实现者注意：弹窗交互抽为 `_confirm_import_mode() -> str`（"merge"/"reset"/"cancel"），
测试 monkeypatch 该方法，不真弹窗。

## 5. 用户实测清单（交付说明必附）

1. 翻译若干行 → 不写回 → 重新导入同一文件 → 选「合并导入」→
   **预期**：日志「继承旧译文 X 行」，审校页译文仍在；
2. 同上但选「清空重导」→ **预期**：日志显示丢弃统计，`bak/` 有 project 快照，审校页全 PENDING；
3. 同上选「取消」→ **预期**：界面与 project.json 均无变化；
4. 术语表填了条目 → 走一次清空重导 → ⚙设置术语表页 → **预期**：术语仍在；
5. 工程为空时导入 → **预期**：无弹窗直接导入（回归）。

## 6. 交付

- 分支 `task/W5` 继续；red→green；交付说明含 pytest 输出 + 上述实测清单 + git log；
- 完成后交总控审核。

---

## 附：总控备注（不在本任务范围，但记录）

- `write_back` 本身不会覆盖未译内容：`regenerate` 只替换已译行的引号文本，
  未译行/PENDING 与游戏文件其他结构逐字节保留，且写回前整文件备份到
  `bak/game/<时间戳>/`——写回是安全的，本任务修的是**导入**侧的数据丢失。
- 用户应急：现有未写回的翻译成果，手动复制 `project.json` 到别处即可保住。
