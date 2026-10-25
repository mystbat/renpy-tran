# 交付说明：合并导入 uid 撞车事故修复（uid 分配稳定化）

- 任务编号：**BUGFIX-2026-10-24-merge-uid**（用户实测严重事故）
- 交付日期：2026-10-24
- 交付性质：**事故修复 + 设计修订**（用户裁决方案）
- 提交：`7bd5f1d`（工作区干净，仅此一提交在 `master`）
- 审核结论：待审核

---

## 1. 背景与事故

### 1.1 事故经过（用户实测）

用户操作路径：导入文件 → 翻译 → 写回 → 点「合并导回」重新导入 → 翻译 → 再次写回。

结果：**15 个翻译文件全部被破坏**——译文与原文张冠李戴。典型样本
（`common.rpy` 第 0 对）：

```
old "Self-voicing disabled."
new "恐怕不容易。最直接的路线将会穿过森林。"   ← 完全无关句子的译文
```

### 1.2 损坏量化（写回后 vs 写回前快照）

| 文件 | 损坏类型 | 规模 |
|---|---|---|
| common / data / exp-1-3somes / screens | 内容错位 | 全部 strings 对 |
| exp-wos | 内容错位 | 2322 对 |
| 其余 10 个 | 仅排版空行膨胀（内容正确） | — |

### 1.3 数据恢复（已完成）

- 恢复源：`bak/game/20261024-220747/tl/chinese2/`（写回前快照，15 文件齐全）
- 动作：15 个 `.rpy` 逐字节恢复；15 个 `.rpyc` 缓存删除（待 Ren'Py 重编译）
- 校验：**SHA256 与快照全部一致 ✓**
- 恢复时机：先量化损坏面 → 立即恢复 → 再定位根因（止损优先）

## 2. 根因

「合并导回」（`ui._merge_import`）走 `backfill.match_translations`，
其第一级匹配是 **uid 精确匹配**——这是设计意图（uid 是唯一主键）。

但 `build_project` 每次导入都把 uid **从 1 重新编号**，uid 实际只是
「单次导入内的段序号」，跨导入毫无语义：

```
旧工程 uid=5（common.rpy 第 5 段，译文「自动朗读已禁用。」）
   ↓ uid 精确命中（主键匹配，设计如此）
新工程 uid=5（完全不同的另一段）
   ↓ 旧译文写入 + 置 REVIEWED
用户写回 → 15 个文件译文按错位 uid 配回文件 → 全面污染
```

**结论：bug 在 uid 分配（不稳定），不在匹配逻辑（uid 主键是对的）。**

## 3. 修复方案（用户裁决）

用户明确指出：「一直都是用 uid 来确定唯一性的。你应该做的是合并导入后的
uid 和之前的 uid 不撞车，比如采用文件名/编号+uid 的方式」——按此实现。

### 3.1 新增 `pytran/uids.py`：稳定 uid 分配器

```
uid = 文件路径 blake2b(16bit) hash << 16 | 文件内段序号(16bit)
```

| 性质 | 说明 |
|---|---|
| 跨导入稳定 | 同文件同一条段，任何次导入得到同一 uid（文件内容不变时）→ 合并导入 uid 直接对位，零猜测 |
| 跨文件唯一 | 不同文件路径 hash 不同 → 多文件导入天然全局唯一（修复 2026-10-21「10 文件 1468 段仅 436 唯一 uid」撞车） |
| 利于写入 | 引擎 seg_map / 预览回查 / 审校游标全部仍走 uid 主键，无间接层 |
| 新旧可区分 | 新 uid 恒 ≥ 1<<16；旧格式（1..N 顺序号）可自动检测迁移 |

### 3.2 改动清单

| 文件 | 改动 | 性质 |
|---|---|---|
| `pytran/uids.py` | **新增**。`path_hash` / `make_uid` / `assign_uids` / `assign_uids_flat` | 新增 |
| `pytran/project.py` | `build_project` 默认走稳定分配器；新增 `reassign_uids=False` 退出（CSV 回填同会话场景保留调用方 uid）；`JsonProjectStore.load` 检测旧格式 uid 自动迁移（按 文件+出现顺序 重编，内容不丢） | 修改 |
| `pytran/backfill.py` | `match_translations` 的 `use_uid_matching` 恢复默认 **True**（uid 已稳定，主键地位恢复）；uid 不可信来源（外部乱序 CSV）可显式关闭退特征码兜底；docstring 记录 DECISION(stable-uid) | 修改 |
| `pytran/ui.py` | `_merge_import` 回归纯 uid 对位（去掉上一轮误加的原文匹配层）：命中 → 保留 target + REVIEWED；未命中 → 「遗留译文」日志；日志区分「uid 对位 / 兜底命中 / 遗留」三种来源 | 修改 |
| `pytran/parser.py` | `regenerate` 段索引加固（BUGFIX(regenerate-uid-index)，见 §3.4） | 修改 |
| `pytran/unren.py` | 写回前 locator 重复检测告警（见 §3.4） | 修改 |

### 3.3 上一轮误修已回滚

事故当天我曾按「禁用 uid 匹配 + 原文匹配」的方向改过一版（`3a22ed1`），
用户指出方向错误后已在本提交中**完全回滚并重建**为 uid 稳定化方案。
`match_translations` 的 uid 匹配恢复默认开启；`_merge_import` 不再做
任何原文/相似度猜测。该中间提交保留在历史中（可追溯修正过程），
最终状态以 `7bd5f1d` 为准。

### 3.4 写回链路同步加固（BUGFIX(regenerate-uid-index)）

用户追问「写回也照着这个处理了吗」引出的边界修复。核查结论：

**写回本来就不依赖 uid**——`regenerate` 按 locator（`# game/<文件>:<行号>`）
逐行定位段，`unren.write_back` 按文件循环 parse→regenerate。事故中写回
「坏」是因为合并导入把段的 target 写错了，写回本身忠实地把错 target
写进了该段 locator 指向的行。**因此 uid 修好后写回自动正确，无需改动
主定位机制。**

但核查发现一个未防护的边界：`regenerate` 原实现用
`{(seg.file, seg.locator): seg}` 字典建索引——**同 locator 多段时后者
静默覆盖前者**（实测：`line one` 行错拿 SECOND，FIRST 丢失）。正常解析
不会产生重复 locator，但异常数据（手工编辑 project.json / 旧工具残留）
下这是又一种「写回静默错位」形态。加固：

| 位置 | 改动 |
|---|---|
| `parser.regenerate` | 段索引按 locator 分组、组内 **uid 升序**取第一段——撞车时确定性仲裁（uid 小的胜出），不再是谁后谁赢；第一段无译文 → 该行保持原样，**不允许同 locator 后段补位**（无译文是明确信号） |
| `unren.write_back` | 写回前 `_warn_duplicate_locators` 扫描同文件内 locator 重复并打 WARNING（含示例），让用户在写回**前**发现异常数据 |

## 4. 测试

### 4.1 新增用例

**`tests/test_stable_uid.py`（11 用例）**

| 用例 | 验证点 |
|---|---|
| `test_uid_stable_across_calls` | 同 (路径, 序号) 跨调用同 uid |
| `test_uid_unique_across_files` | 50 文件同序号零碰撞 |
| `test_uid_seq_stable_within_file` | 同文件序号有序唯一 |
| `test_uid_high_bit_distinguishes_from_legacy` | 新旧格式可区分 |
| `test_build_project_stable_uids_same_file` | 两次导入同文件 uid 完全一致 |
| `test_build_project_multi_file_no_collision` | 多文件全局唯一 |
| `test_build_project_preserves_file_order` | 序号 = 文件内出现顺序 |
| `test_reassign_uids_opt_out` | opt-out 保留调用方 uid |
| `test_merge_import_uid_alignment` | **核心回归**：两次导入同文件，旧译文精确落到同段、其它段不受影响 |
| `test_merge_import_no_cross_file_leak` | 跨文件零泄漏（A 文件译文不落 B 文件段） |
| `test_load_migrates_legacy_uids` | 旧工程（1..N uid）加载迁移，内容不丢 |

**`tests/test_backfill_merge_uid.py`（5 用例）**：uid 匹配默认开启 /
稳定分配杜绝撞车 / 可关闭退特征码 / 特征码不受影响 / 单段单译。

**`tests/test_regenerate_locator_collision.py`（4 用例，§3.4 配套）**：

| 用例 | 验证点 |
|---|---|
| `test_locator_collision_takes_lowest_uid` | locator 撞车 → uid 升序第一段胜出（确定性） |
| `test_locator_collision_without_target_skipped` | 第一段无译文 → 行保持原样，后段不补位 |
| `test_normal_case_unaffected` | 正常无撞车场景按各自 locator 精确写回 |
| `test_write_back_warns_on_duplicate_locators` | 写回前对重复 locator 打警告 |

### 4.2 更新的既有用例

- `tests/test_backfill.py`：`test_match_by_uid_exact` /
  `test_match_uid_takes_precedence_over_feature` 恢复原断言（uid 主键）；
  新增 `test_uid_matching_opt_out_for_untrusted_source`；
- `tests/test_parser_roundtrip.py::test_multi_file_import_uid_unique`：
  断言从「重编 1..N」改为「稳定 hash 派生」。

### 4.3 回归结果

```
python -m pytest tests -p no:cacheprovider
→ 477 passed
```

（基线 461 → 新增 16 用例，全绿。）

## 5. 影响面评估

| 面 | 评估 |
|---|---|
| uid 语义 | 从「导入内序号」变为「路径 hash + 段序号」的全局稳定 id。**project.json 中的 uid 数值会变**——旧工程首次打开自动迁移，段内容/顺序/状态不变 |
| 引擎/预览/审校 | 全部仍走 uid 主键，无逻辑改动；uid 唯一性从此有分配层保证 |
| 写回 | 不变（regenerate 按 locator 定位；uid 只做 seg_map 键） |
| CSV 导入导出 | `import_csv` 先用 by_uid 直配（同会话 uid 未变，正常）；`export_csv` 输出新 uid——**跨工具手工编辑 CSV 后再导入，uid 列会被按新规则重编，匹配走特征码/相似度兜底**（原设计即如此） |
| 2.0 迁移（migrate.py） | 走 `build_project` 自动获稳定 uid，无感 |
| 数据兼容 | 旧 project.json 自动迁移；无需人工干预 |

## 6. 审核要点建议

1. **`pytran/uids.py` 的 hash 碰撞风险**：16bit 路径 hash × 单文件段序号
   16bit。同一次工程内文件路径不同即 hash 不同；理论上两路径 hash 碰撞
   （1/65536）且段序号也相同时才会撞 uid——建议评估是否可接受，或
   升到 32bit hash（uid 仍为 Python int，无溢出问题）；
2. **单文件段数上限 65535**：超过后序号回绕会同文件内撞车——建议评估
   是否需要 slot 递增兜底（代码已留 `_MAX_SEQ_PER_SLOT` 常量位置）；
3. **旧工程迁移的正确性**：按 (文件, 出现顺序) 重编，若旧工程段顺序与
   当前磁盘文件不一致（用户曾手工增删行），迁移后 uid 与下次导入的
   uid 可能错位——迁移时会打 info 日志，建议人工抽查一次；
4. **`_merge_import` 的「遗留译文」日志**：uid 未命中时用户能否感知并
   处理（当前只打日志）。

## 7. 交付物清单

| 文件 | 类型 |
|---|---|
| `pytran/uids.py` | 新增（稳定 uid 分配器） |
| `pytran/project.py` | 修改（build_project 稳定分配 + load 迁移） |
| `pytran/backfill.py` | 修改（uid 匹配默认恢复 + DECISION 记录） |
| `pytran/ui.py` | 修改（_merge_import 回归纯 uid 对位） |
| `pytran/parser.py` | 修改（regenerate locator 撞车确定性仲裁） |
| `pytran/unren.py` | 修改（写回前 locator 重复告警） |
| `tests/test_stable_uid.py` | 新增（11 用例） |
| `tests/test_backfill_merge_uid.py` | 新增（5 用例） |
| `tests/test_regenerate_locator_collision.py` | 新增（4 用例） |
| `tests/test_backfill.py` | 修改（3 处断言） |
| `tests/test_parser_roundtrip.py` | 修改（1 处断言） |
| `docs/BUGFIX-merge-uid-collision.md` | 修订（第二轮，含事故/方案/测试/设计说明） |

> 注：代码改动已包含在提交 `7bd5f1d` 及其后的工作区修改中；
> 本交付说明与最终代码一同提交（审核通过后）。
