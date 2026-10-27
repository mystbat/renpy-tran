# 交付说明：strings 块跨文件错位（用户实测反馈，2026-10-25）

> 状态：**已修复，未提交**。等待审核。
> 分支：`feature/enable-thinking-toggle`
> 涉及文件：`pytran/backfill.py`、`pytran/ui.py`，新增 2 个测试文件。

---

## 1. 用户报告的两个现象

| # | 用户原话 | 现象 |
|---|---|---|
| ① | 「查询现在的工作区中，是否有这样一段 old "Take me to the Sylphir Island"，我在审校界面的待翻译并没看到它。翻译后并写回后，我看到这段变成了 new "{image=it_qu.webp} 波泽拉的水晶球"」 | 一条本该待译的 strings 行，变成了完全无关的译文 |
| ② | 「未翻译前，我人工数了有20个待翻译内容。在审校界面只看到19个」 | 待翻译数量少了 1 条 |

**这两个是同一个 bug 的两张脸**，不是两个独立问题。

---

## 2. 根因

### 2.1 直接原因：`backfill.match_translations` 第 2 级没有文件约束

`match_translations` 是「外来译文 → 工程 Segment」的兜底匹配链（§6.2），三级：

1. `uid` 精确
2. 特征码归一化后等值
3. 定位符相同 + `SequenceMatcher` 相似度 ≥ 0.92

原实现第 2 级：

```python
feat_index.setdefault(normalize_feature(s.feature), []).append(s)   # 只按 feature 建桶
...
cand = feat_index.get(normalize_feature(row.feature))               # 取桶
seg = take(cand)                                                    # 取第一个未被占用的
```

**关键事实**：Ren'Py 的 `strings` 块特征码是**常量**——`translate chinese2 strings:`，与文件内容无关。所以同一目标语言下，**所有文件**的 strings 段归一化后特征码完全相同，全部塌进同一个桶。`take()` 按 FIFO 派发，只保证「一个段不被占用两次」（`consumed` 集合），**完全不保证「不跨文件」**。

于是 A 文件的译文被按位置发给了 B 文件的段。

### 2.2 事故现场还原

```
tl/chinese2/images/dlc_aurora/dlc.rpy    strings 块 @22493，43 对，已译
tl/chinese2/exp-1-3somes.rpy             strings 块 @14865，27 对已译
                                         strings 块 @15421， 1 对待译（new == old）
```

两处 feature 都是 `translate chinese2 strings:` → 同桶。

`dlc.rpy` 前 28 条译文被**按位置、一一对应、按顺序**写进了 `exp-1-3somes.rpy` 的 28 条 strings 段：

| exp 的 source | 被写入的 target | 实际来自 |
|---|---|---|
| `Watch Kara try to eat out Teriania` | `海贝狂战士` | dlc.rpy 第 1 条 |
| `Service both girls at the same time` | `在她脸上做` | dlc.rpy 第 6 条 |
| … | … | … |
| `Take me to the Sylphir Island` | `{image=it_qu.webp} 波泽拉的水晶球` | dlc.rpy 第 28 条 |

`dlc.rpy` 第 28 条原文是 `{image=it_qu.webp} CRYSTAL BALL FROM PORZELLA` —— 就是用户看到的「波泽拉的水晶球」。**这不是翻译错误，是错位。**

### 2.3 为什么现象 ② 会「少一条」

`parser._emit_old_new_pair` 的判定：

```python
translated = bool(target) and target != source.strip()
```

`Take me to the Sylphir Island` 这条在文件里 `new` 与 `old` 相同 → 本应 `PENDING`。但错位后被塞进了一个非空且不同的译文 → `translated = True` → 状态变 `REVIEWED`。

而「待翻译」筛选是纯状态等值：

```python
# ui_state._filter_segments
return [s for s in status if s.status == SegmentStatus.PENDING]
```

`REVIEWED` 不满足 → 从列表消失。

**用户的 20 是对的，界面显示的 19 才是错的。**

### 2.4 为什么导出熔断没拦住（第二处缺陷）

熔断 `evaluate_export_fuse` **确实检出了**问题。对当前 `project.json` 实测：

```
tag_check: 14 条   （28 条里有 14 条 target 的标签/插值与 source 不符）
failed:     1 条
```

但 UI 的写回入口**绕过了熔断**：

```python
# pytran/ui.py  _on_writeback（修复前）
self.deps.exporter.write_back_project(self.project, self.work_dir, journal)
```

`write_back_project` 直接调 `unren.write_back`，**不经过 `export_rpy` 里的 `evaluate_export_fuse`**。所以 14 条已知的错译文照样落盘。

用户直到肉眼看见 `new "{image=it_qu.webp} 波泽拉的水晶球"` 才发现——这说明「检出问题」和「阻止问题」之间断了一环。

---

## 3. 修复

### 3.1 `pytran/backfill.py`：第 2/3 级按文件隔离

新增两个按文件分键的索引：

```python
feat_path_index[(归一化特征码, 完整相对路径)]
feat_name_index[(归一化特征码, 文件名)]
```

第 2 级改为走 `feature_candidates(row)`：

- 能从定位符解析出文件 → **只在该文件内取候选**；该文件没有这个特征码的段 → 直接判未命中，**绝不跨文件借用**；
- 定位符解析不出文件（旧数据）→ 才退回原全工程特征码桶，保持向后兼容。

新增 `locator_file()`，从 `# game/<rel>:<line>` 反推文件路径（CSV 没有文件列，只能从定位符取）。

第 3 级相似度同样加文件一致性校验：定位符字符串相同不代表同一文件（不同目录可以同名同号），双方都能解析出文件时必须一致。

`_file_keys()` 同时返回「完整路径」和「文件名」两个键，用于兜住历史快照里出现过的路径前缀不一致（有一次快照的定位符少了 `tl/chinese2/` 前缀）。

### 3.2 `pytran/ui.py`：写回前先过熔断

`_on_writeback` 改为：

1. 先跑 `evaluate_export_fuse`；
2. 有阻止项 → 弹确认框，列出**哪几类、各多少条**，并附**原文/译文明显不对应的样例**（让用户一眼看出是错位而不是漏译）；
3. 用户选「否」→ 不写回；
4. 用户选「是」→ 强制写回（保留 §6.3「用户可强制导出」的既有语义）；
5. 熔断通过 → 不打扰，直接写回。

---

## 4. 验证

### 4.1 用真实事故数据验证（不是构造数据）

拿**未被污染的写回前快照** `bak/game/20261025-185049/tl/chinese2/exp-1-3somes.rpy` 解析，得到 **666 段**，其中 **20 条 PENDING**：

- 19 条 `dialogue`，定位符 `:15311` … `:15419`（新增的 Sylphir Island 剧情）
- 1 条 `strings`，定位符 `:15425`，source = `Take me to the Sylphir Island`

**正好是用户人工数出的 20 条。**

再拿 `dlc.rpy` 的 42 条已译 strings 行去匹配这 28 条 strings 段：

| | 修复前 | 修复后 |
|---|---|---|
| `matches` | 28 | **0** |
| `unmatched` | 0 | **42** |
| 跨文件命中 | 28 | **0** |
| exp 段被污染数 | 28 / 28 | **0 / 28** |

### 4.2 对照游戏目录里已被污染的文件

```
干净快照(写回前)  ：666 段，{translated: 646, pending: 20}，待翻译 20 条
游戏目录(已污染)  ：666 段，{translated: 666}，          待翻译  0 条
```

`Take me to the Sylphir Island` 在干净快照里 `tgt=None / pending`，在游戏目录里 `tgt='{image=it_qu.webp} 波泽拉的水晶球' / translated`。**这就是用户看到的那一幕。**

### 4.3 熔断实测

对当前 `project.json`（已被污染的状态）跑 `evaluate_export_fuse`：`tag_check` 14 条、`failed` 1 条。其中末条就是 `Take me to the Sylphir Island → {image=it_qu.webp} 波泽拉的水晶球`。

修复后这条会在写回前被弹框拦下，并展示原文/译文对照。

### 4.4 测试

新增 **16** 个用例：

| 文件 | 数量 | 守什么 |
|---|---|---|
| `tests/test_backfill_cross_file_strings.py` | 10 | 跨文件不泄漏；同文件仍正常匹配；同文件内顺序派发不变；路径前缀不一致时按文件名兜底；第 3 级相似度也按文件隔离；uid 优先级不受影响；复刻 28 对 28 的完整场景 |
| `tests/test_ui_writeback_fuse.py` | 6 | 熔断能检出 tag 不符；干净工程不弹框；用户选「否」绝不写回；用户选「是」允许强制；确认框文案含分类条数与原文/译文对照 |

**全量：644 passed**（修复前 628，新增 16）。`EXITCODE=0`。

---

## 5. 对现有行为的兼容性

| 场景 | 影响 |
|---|---|
| 同文件内的 strings 回填 | **不变**，仍按顺序派发 |
| uid 精确匹配 | **不变**，仍是最高优先级（uid 本身含路径 hash，天然文件隔离） |
| 定位符无法解析文件的旧数据 | **不变**，退回全工程特征码桶 |
| 路径前缀不一致的历史快照 | **变好**，新增文件名兜底 |
| 对话段（feature 带唯一 hash） | 基本不变；同名 label 跨文件时现在会正确隔离 |
| 强制写回 | **保留**，但需要用户明确确认 |

---

## 6. 遗留与建议（未做，供审核决策）

1. **用户工作区仍是污染状态**。`project.json` 与游戏目录 `game/tl/chinese2/exp-1-3somes.rpy` 里那 28 条 strings 译文仍是错的。修复只防止**再次发生**，不会自动回滚已有污染。建议恢复路径：用干净快照 `bak/game/20261025-185049/` 覆盖游戏目录，重新导入，即可回到「20 条待译」的正确状态。**这一步涉及改动用户的游戏文件，我没有擅自执行。**
2. **`test_same_source_different_file_no_cross_match` 之前是假通过**。它只断言 `len(out.matches) == 1`，没断言命中的是哪个文件，所以跨文件泄漏一直存在却没被测出。建议后续把这类断言统一改为「同时断言 `m.segment.file` 与 `m.method`」。
3. **`_emit_old_new_pair` 的 `translated` 判定值得加固**。`bool(target) and target != source` 在孤立看是对的，但它让「被塞进错译文」和「人工真的翻了」变得不可区分。若在导入时对 `tags_ok` 不通过的段降级为 `PENDING` 并记 `error`，现象 ② 会更早暴露。
4. **`_refresh_review()` 性能问题未修**（上一轮已记录）：2000 行约 15.0 s，瓶颈在此函数。
5. **`pytran/exporter.py:import_csv` 的 `pool` 是全工程段**。修复后不会跨文件错配了，但从设计上，CSV 导入时若能用 CSV 里已有的路径信息把 `pool` 收窄到同文件，会更快也更稳。

---

## 7. 改动清单

```
M  pytran/backfill.py    +91  第 2/3 级按文件隔离；新增 locator_file / _file_keys
M  pytran/ui.py          +90  写回前先过 §6.3 熔断，明细 + 样例 + 二次确认
A  tests/test_backfill_cross_file_strings.py   +10 用例
A  tests/test_ui_writeback_fuse.py              +6 用例
```

未提交。等待审核。
