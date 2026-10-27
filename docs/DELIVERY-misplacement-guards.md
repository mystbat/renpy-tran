# 交付说明：错位 / 错误覆盖类缺陷的根因与防护（需求 #5 #6 #7）

> **⚠️ 部分废止（SUPERSEDED, 2026-10-26）**：本文档的防护方案
> （feat_path_index / feat_name_index / `_same_file` / `_same_source`
> 四道守卫）已随 `match_translations` 三级兜底链**整体删除**，由
> `pytran.identity` 段身份体系（键 = 完整文件路径 + 原文）替代——
> 这些守卫补的洞在新构造下不存在。根因分析部分（§2 起）仍然有效。
> 见 `docs/TASK-segkey-identity-refactor.md`（v3.1）。

- 日期：2026-10-25
- 分支：`feature/enable-thinking-toggle`（供审核人员合并）
- 状态：**代码已改完、全量测试通过、未提交**
- 对应需求：
  - #5「待翻译里的 `old "Take me to the Sylphir Island"` 翻译写回后变成 `new "{image=it_qu.webp} 波泽拉的水晶球"`」
  - #6「人工数有 20 个待翻译，审校界面只看到 19 个」
  - #7「设计多种测试方案，测试导入、合并导入、写入后导入等流程，是否还会出现错位、错误覆盖」

---

## 1. 结论先说

#5 和 #6 **不是翻译质量问题，是匹配错位**。两句话共用一个根因：
`backfill.match_translations` 的第二级「特征码匹配」**没有文件边界**。

修复分三层，全部落在「宁可判不匹配，也不猜」的原则上：

| 层级 | 缺陷 | 修复 |
|---|---|---|
| 特征码级 | 跨文件串译（#5 #6 的直接成因） | `feature_candidates` 按文件收敛，绝不跨文件借候选 |
| uid 级 | `path_hash` 仅 16 bit，会碰撞 → 跨文件覆盖 | `_same_file` 校验文件 |
| uid 级 | 源文件增删行 → uid 漂移 → 同文件内错位 | `_same_source` 校验原文 + 相似度下限 |
| CSV 导入 | uid 命中即盲写，完全不看原文 | `_same_source` 校验原文 |

---

## 2. 根因一：特征码匹配没有文件边界（#5 #6）

### 2.1 机制

`strings` 类段落有一对一无法区分的特征码——整块共用一个常量：

```
translate chinese2 strings:
```

`match_translations` 原来把 `feature` 当作索引键建全局字典：

```python
feat_index: dict[str, list[Segment]]   # 常量特征 → 15 个文件的全部 strings 段
```

于是 `dlc.rpy` 的 27 条译文按 **FIFO** 派发给了 `exp-1-3somes.rpy` 的 27 个 strings 段。

### 2.2 数据证据

- 未污染的写回前文件 `bak/game/20261025-185049/tl/chinese2/exp-1-3somes.rpy` 解析出 **666 段**
  （`dialogue: 638`、`strings: 28`），状态 `{TRANSLATED: 646, PENDING: 20}`。
- 那 20 条 PENDING = **19 条 dialogue**（locator `:15311` … `:15419`）+ **1 条 strings**（`:15425`，原文 `Take me to the Sylphir Island`）。
  **这正是用户手工数出的 20**。
- 写回后该段被塞入 `{image=it_qu.webp} 波泽拉的水晶球`，状态翻成 `REVIEWED`，所以审校界面只剩 **19** —— #6 的「19 vs 20」由此而来。
- 那 27 条错误译文全部来自 `dlc_aurora/dlc.rpy` 的 strings 块（首 27 个 `new` 值逐一对应）。

### 2.3 修复

`feature_candidates(row)` 改为三级，且**永不跨文件**：

```python
feat_path_index: dict[tuple[str, str], list[Segment]]  # (完整路径, 特征码)
feat_name_index: dict[tuple[str, str], list[Segment]]  # (文件名, 特征码)
# 都取不到 → 返回 None，交给下一级；绝不退化成全局 feat_index
```

只有 locator 完全解析不出文件名时，才允许用旧的全局 `feat_index`。

---

## 3. 根因二：uid 会碰撞（跨文件错误覆盖）

`pytran/uids.py` 的 `uid = (path_hash & 0xFFFF) << 16 | (seq & 0xFFFF)`，`path_hash` 是 **blake2b 2 字节**，只有 16 bit。

**实测**：3000 个合成路径产生 **59 次碰撞**（2941 个唯一 hash；理论期望 `3000²/2¹⁷ ≈ 68`）。

真实可复现的一对：

```
tl/chinese2/file_56.rpy   → path_hash = 40439
tl/chinese2/file_228.rpy  → path_hash = 40439
两者 seq=1 的 uid 都是 2650210305
```

原实现「uid 命中即认定」，工程里两文件先后顺序不同就会把译文写进另一个文件。

### 修复

`take_uid` 增加 `_same_file` 校验：比对 `locator_file(row.locator)` 与 `seg.file`（完整路径优先，退化为文件名比较；任一侧无法解析则不阻断）。

---

## 4. 根因三：uid 漂移（同文件内错位）

uid 由**文件内序号**推导。源文件插入/删除一行，其后所有段的 uid 整体平移。
`_merge_import` 只按 uid 对位（`DECISION(stable-uid, 用户裁决 2026-10-24)`），于是：

```
旧文件: 1=LINE1  2=LINE2  3=LINE3  4=LINE4  5=LINE5
新文件: 1=LINE1  2=LINE2  3=NEW    4=LINE3  5=LINE4  6=LINE5
                 ↑ uid=3 现在是 NEW，却拿到了 LINE3 的译文
```

### 修复（两级容差）

`take_uid` 对同文件候选再校验原文：

1. 原文**精确相等** → 命中，`method="uid"`
2. 原文相似度 ≥ `UID_SIMILARITY_FLOOR = 0.80` **且长度 ≥ `UID_SIMILARITY_MIN_LEN = 12`** → 命中，记 `logger.info`
3. 否则 → 拒绝，`logger.warning` + 降级到特征码级按**原文**重新定位

**为什么要长度门槛**：`SequenceMatcher` 在短串上极不灵敏。实测 `'LINE2'` vs `'LINE3'` 相似度**恰好 0.80**，正好骗过下限。删除一行导致 uid 前移时，`LINE2` 的译文就落到了 `LINE3` 上——测试场景 D 抓到的就是这个。短原文改一两个字符就是另一句话，必须精确相等。

### 同时删掉的一层危险兜底

特征码级原先有一层「原文不等就按位置取第一个」的兜底，理由是「兼容人工微调过原文的场景」。测试场景 D 证明它直接制造错位：把 `LINE2` 的译文塞给 `LINE3`、`LINE3` 的塞给 `LINE4`，连续三条。**已删除**——原文对不上就是不匹配，宁可留给人工审校。

---

## 5. 根因四：CSV 导入的 uid 通道完全无防护

`ExporterImpl.import_csv` 原实现：

```python
for uid in set(incoming) & set(by_uid):
    seg.target = incoming[uid]["target"]   # ← 完全不看原文
```

CSV 里的 uid 是**导出时**的快照。源文件之后若增删过行，uid 已漂移，盲写就是把上一句的译文盖到下一句上。测试场景 J 抓到：插入一行后导入旧 CSV，得到 `{'INSERTED': '猫', 'Cat': '狗', 'Dog': None}`——整体错一位。

### 修复

uid 通道先过 `_same_source`；原文不符则 `logger.warning` 并把它降级到 §6.2 兜底链按原文重新找位置。

---

## 6. 测试方案（需求 #7）

新增 `tests/test_pipeline_no_misplacement.py`，**18 个用例**，11 组场景。
设计原则：**只断言「译文落在哪一段」**，绝不断言匹配数量——历史事故的特征恰恰是数量完全正常（`matches=666 unmatched=0`）而内容全错。

| 场景 | 用例 | 覆盖点 |
|---|---|---|
| A 全新导入 | `test_A_fresh_import_assigns_uids_and_places_translations`<br>`test_A_uid_is_stable_across_independent_imports` | uid 分配、同内容重复导入 uid 稳定 |
| B 合并导入（不变） | `test_B_merge_import_unchanged_is_lossless` | 基线：100% 按 uid 命中 |
| C 合并导入（插入行） | `test_C_merge_import_with_insertion_does_not_misplace`<br>`test_C_uid_really_does_drift_on_insertion` | uid 后移不错位；并证明「确实漂移」以免用例失效 |
| D 合并导入（删除行） | `test_D_merge_import_with_deletion_does_not_misplace` | uid 前移不错位；被删行的译文进 unmatched |
| E 合并导入（重排） | `test_E_merge_import_with_reorder_follows_source_not_index` | 译文跟原文走，不跟序号走 |
| F 合并导入（改写） | `test_F_merge_import_with_rewritten_line_does_not_reuse_old_target` | 改写句不得套旧译文 |
| G 写回后重导入 | `test_G_writeback_then_reimport_is_idempotent`<br>`test_G_writeback_roundtrip_preserves_targets_exactly` | regenerate → parse → merge 幂等，uid/译文不漂 |
| H 多文件 strings | `test_H_strings_across_files_do_not_cross_match`<br>`test_H_strings_same_file_still_match_by_source` | 复现 #5：跨文件必须全 unmatched；同文件仍要能匹配 |
| I path_hash 碰撞 | `test_I_path_hash_collision_does_not_cross_write`<br>`test_I_hash_collision_rate_is_bounded` | 两文件同 uid 不跨写；把「16 bit 必碰撞」固化成事实 |
| J CSV 往返 | `test_J_csv_roundtrip_does_not_misplace`<br>`test_J_csv_with_shifted_uid_falls_back_to_source` | CSV 往返；uid 漂移后按原文兜底 |
| K 熔断 | `test_K_fuse_blocks_misplaced_target`<br>`test_K_fuse_passes_clean_project` | 错位译文写回前被拦住；正常工程不误伤 |

核心断言辅助函数 `_assert_all_landed_correctly`：对每条命中同时校验
`m.segment.source == m.row.source` **和** `m.segment.file == locator_file(m.row.locator)`。

---

## 7. 验证结果

### 7.1 全量回归

```
737 passed（tests/ 全绿）
```

### 7.2 真实事故数据（单文件 666 段）

用未污染的写回前文件重解析，配旧快照的 646 条已译行：

```
matches=646  unmatched=0
方式分布 = {'feature': 646}
原文不一致（错位） = 0 条
跨文件覆盖       = 0 条
uid 漂移         = 0/646
```

### 7.3 全工程规模（15 文件 / 12241 段）

```
可回填行 = 12068
matches=12068  unmatched=0
方式分布 = {'uid': 12068}      ← 正常路径全部走 uid，未被新校验误伤
原文不一致（错位） = 0 条
跨文件覆盖       = 0 条
strings 段 1736 个，错位 0 条
```

### 7.4 修复前后对比（合成用例）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 两文件 path_hash 碰撞，B 文件排在前 | **写错文件** | 写对（两种顺序都正确） |
| 插入一行致 uid 漂移，5 条译文 | **3/5 错位** | **5/5 正确**（前 2 条走 uid，后 3 条自动降级为特征码+原文） |
| 删除一行，5 条译文 | 连续 3 条错位 | 全部正确 |
| 旧 CSV 遇到已漂移的 uid | 整体错一位 | 按原文找回位置 |

---

## 8. 需要审核人员注意的行为变更

1. **合并导入的「兜底命中」会变少**。原文被人工改过的行不再被自动继承，会进 unmatched（日志「N 行旧译文未匹配」）。这是有意的：那些译文本来就可能挂错了句子。
2. **`UID_SIMILARITY_MIN_LEN = 12`** 是可调常量。若审核认为短句也需要宽容，改这一个值即可，但请先复现场景 D 确认不会重新引入错位。
3. **`tests/test_ui_import_safety.py` 夹具已修正**。原夹具里旧段 `source="src 1"`、新解析出 `"source line for pytran import-safety test."`，两者矛盾却期望「同 feature 即继承」——靠的正是本次被修掉的宽松语义。现由 `_source_for(feature)` 统一生成，模拟的才是真正的「同一段重新导入」。
4. **`test_same_source_different_file_no_cross_match` 这类老用例只断言 `len(matches)`**，无法发现内容错位。新增用例已改为断言内容，建议后续 review 时对同类断言一并收紧。

---

## 9. 未处理 / 遗留

- **用户工作区仍处于污染状态**：本次修复只阻止再次发生，**不会自动回滚**已写错的 28 条 strings 译文。
  恢复路径（需用户确认，会改动游戏文件）：用 `bak/game/20261025-185049/` 覆盖游戏目录 → 重新导入 → 回到 20 条待翻译。
- `_refresh_review()` 在 2000 行规模下约 15.0 s，UI 卡顿未彻底解决，与本次缺陷无关。
- 未提交任何改动，等审核。

---

## 10. 改动文件清单

| 文件 | 改动 |
|---|---|
| `pytran/backfill.py` | `_same_file` / `_same_source` / `take_uid` / `take_matching` / `feature_candidates` / `locator_file`；新增常量 `UID_SIMILARITY_FLOOR`、`UID_SIMILARITY_MIN_LEN`；删除特征码级按位置兜底 |
| `pytran/exporter.py` | `import_csv` uid 通道增加 `_same_source` 校验；新增 `_same_source` 辅助函数 |
| `pytran/project.py` | 文档字符串纠正「path_hash 不同即不撞车」的错误表述 |
| `tests/test_pipeline_no_misplacement.py` | **新增**，18 个用例 / 11 组场景 |
| `tests/test_backfill_cross_file_strings.py` | 新增 3 个 uid 校验用例，替换旧的「uid 盲胜」用例 |
| `tests/test_backfill.py` | 新增 `test_uid_hit_rejected_when_source_is_a_different_line`，修正 2 个旧用例数据 |
| `tests/test_backfill_merge_uid.py` | 修正旧用例数据（原 `source='x'` 测的正是「uid 盲胜」） |
| `tests/test_ui_import_safety.py` | 夹具改为 `_source_for(feature)`，使旧段原文与新解析原文一致 |
