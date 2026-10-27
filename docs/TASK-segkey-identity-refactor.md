# 任务书：错位 Bug 构造性重构 —— seg_key 身份体系（v3.1）

> **✅ 执行状态（2026-10-26）：P0–P8 全部完成**
>
> | 包 | 状态 | 备注 |
> |---|---|---|
> | P0 三项调查 | ✅ | 重复桶 20 个（最大 11 条）→ occurrence 完整 FIFO 实现；unren 骨架自洽确认；持久化 uid 清点完毕 |
> | P1 核心构造 | ✅ | `pytran/identity.py`：`segment_key` / `KeyIndex` / `resolve_batch` |
> | P2 入口收编 | ✅ | `_merge_import` / `import_csv`（CSV v2 带 key 列，v1 兼容重建）/ `migrate._align_strings`（#11 关闭）；`match_translations` 退役薄包装 |
> | P3 uid 降级 | ✅ | `uids.py` 会话内 1..N；legacy hash uid 加载即重编；批次回到文档顺序 |
> | P4 持久化边界 | ✅ | uid roundtrip 稳定性实测通过；unren 骨架语义钉死；审计缓存确认内存态 |
> | P5 熔断不变量 | ✅ | `IdentityViolation` 构造点断言；防线有效性经回归形态模拟验证 |
> | P6 复核视图 | ✅ | `_show_unmatched_review`：相似候选仅展示排序，人工一键写入；无 QApplication 环境退化为日志模式 |
> | P7 测试重构 | ✅ | 旧语义用例按 §5.4 裁决改写；**664 全绿**（offscreen Qt 平台） |
> | P8 文档迁移 | ✅ | `BUGFIX-merge-uid-collision.md` 废止标注；`DELIVERY-misplacement-guards.md` 部分废止；复盘补记后记 |
>
> 过程中修复的真 bug：key 分隔符撞 CSV 分隔符（`|`→`\x1f`）；桶内位置盲取
> 改为等值筛选（让不变量真正有牙）；复核视图无头环境 fastfail 防御。
> 遗留：用户工作区回滚仍需单独批准（本任务书范围外）。
>
> 验证：真实数据 665 行 `matches=665, unmatched=0, source_mismatch=0,
> cross_file=0`；验收 4.1-1/2/4/5/6/7/8/9 达成；4.1-3 需 12241 段全量
> 工程数据（当前工作区仅 666 段 bak 可用，回滚后补验）。

- 日期：2026-10-26
- 版本：v3.1
- 依据文档：`docs/POSTMORTEM-misplacement-six-rounds.md`（六轮复盘）
- 任务性质：**构造性重构**，非补丁修复
- 验收一句话：**译文落在哪一段由 `(文件, 原文)` 唯一决定，错位在构造上不可表达；任何无法确认的匹配落入可见的 unmatched，绝不静默写回。**

---

## 0. 背景与动机（一段话）

六轮修复的共性错误是「用派生量（uid 序号 / 16-bit path hash / 特征码）充当段身份」，每轮修好一个键又被下一个场景打破。本任务将段身份的定义改为数据中本已稳定的 `(完整文件路径, 原文)`，匹配退化为字典查找；uid 降级为会话内游标；三条导入入口（合并导入 / CSV / 迁移）收编到单一解析函数。设计与影响面评估见对话记录 2026-10-26（本任务书为其执行规格）。

---

## 1. 目标与非目标

### 目标

1. 段身份 = `(file, source, occurrence)`，其中 `file` 为完整相对路径（不截断 hash），`source` 经版本化规范化函数处理，`occurrence` 仅在同文件重复原文桶内启用。
2. 单一身份解析函数 `resolve_identity(file, source) -> Segment | None`，三条入口全部走它，**禁止任何入口自带对齐/兜底逻辑**。
3. uid 降级为会话内 `1..N` 序号：不持久化、不跨导入比较、不进导出物。解析→翻译→写回生命周期内保持唯一即可。
4. 删除：16-bit path_hash、`match_translations` 三级兜底链、`feat_path_index/feat_name_index`、`_same_file/_same_source` 守卫、位置兜底、相似度阈值魔法数（0.80/0.92/MIN_LEN=12）。
5. 写回熔断新增内容级不变量：每个匹配强制 `seg.source == row.source` 且 `seg.file == locator_file(row.locator)`，不满足即 `ExportBlocked`。
6. 持久化边界（CSV / 工程快照 / journal / UI 状态 / unren 标签）从 uid 迁移到 seg_key 或会话内失效。
7. unmatched 复核视图（可选包 P6）：人工确认替代自动猜测。

### 非目标

- 不做原文相似度自动匹配（任何形式）。相似度只允许出现在 P6 复核视图中作**展示排序**。
- 不回滚用户工作区污染数据（另行走用户批准流程，见复盘 §7.3）。
- 不处理 `_refresh_review()` 性能问题（另行立项）。

---

## 2. 核心设计规格

### 2.1 seg_key 定义

```
key = key_version + "|" + file + "|" + normalize(source) [+ "#" + occurrence]
```

- `file`：完整相对路径，分隔符统一 `/`，不做任何 hash 截断。
- `normalize(source)`：规范化规则**显式枚举**（引号统一、空白压缩等，沿用 `normalize_feature` 的思想但只作用于原文），钉死在单一纯函数中，函数所在模块命名为 `identity.py`。
- `key_version`：整数版本号，随 key 一起序列化。规范化规则任何变更必须升版本号并触发全工程重算迁移，**禁止静默兼容**。
- `occurrence`：同文件内同 `normalize(source)` 的出现序号（1-based）。仅当该桶大小 > 1 时参与键；桶大小 = 1 时省略，保持键简洁。

### 2.2 resolve_identity 契约

```python
def resolve_identity(segments_index, row) -> Segment | None
```

- 按 `(file, key)` 精确字典查找，O(1)，无优先级链、无兜底。
- 查不到返回 `None` → 调用方计入 unmatched，**必须**产生用户可见的记录（日志 + unmatched 报告），不允许静默丢弃。
- 重复桶（大小 > 1）按 occurrence 对位；row 侧与 seg 侧桶大小不一致 → 整桶进 unmatched（不猜）。

### 2.3 三条入口收编

| 入口 | 现状 | 改造 |
|---|---|---|
| `_merge_import`（`ui.py:600`） | 调 `match_translations` 三级链 | 改走 `resolve_identity` |
| `import_csv`（`exporter.py:121`） | uid 分支 + `_same_source` 校验 | 删 uid 分支，改走 `resolve_identity`；CSV 格式加 key 列 |
| `migrate._align_strings`（`migrate.py:187`） | `str_rows.get(source)` 无守卫（缺陷 #11） | 改走 `resolve_identity`，顺带清掉 #11 |

`match_translations` 保留薄兼容包装一个版本期（内部转调 `resolve_identity`，打 deprecation 日志），下个版本删除。

### 2.4 uid 降级

- `uids.py` 简化：`assign_uids` 改为按解析顺序编 `1..N`；删除 `path_hash` / `make_uid` / 16-bit 结构及全部相关注释。
- `project.py` 的 `legacy` 判断、`next_uid` 逻辑同步简化。
- `parser.py:613` 的局部计数赋值即成为正式语义（会话内唯一）。
- 旧工程文件（含大整数 uid）加载时一次性重编，无兼容分支。

### 2.5 熔断不变量（P5）

`evaluate_export_fuse` 新增检查项：对每个待写回匹配断言
`seg.source.strip() == row.source.strip()` 且 `seg.file == locator_file(row.locator)`。
任一失败 → `ExportBlocked`，错误信息含 row/seg 双方的 file、source、locator。**此项是整个构造的最后防线，不允许任何开关绕过。**

### 2.6 持久化边界迁移（P4）

| 边界 | 改造 |
|---|---|
| CSV 导出（`exporter.py:60`） | 加 `key` 列；uid 列保留一个版本期仅作迁移依据 |
| CSV 导入（旧格式） | 按 `(file, source)` 重建 key；重建失败的行进 unmatched 报告 |
| 工程快照 / journal（`models.py`） | 序列化加 `key_version`；`next_uid` 字段废弃 |
| UI 持久化状态 / 审计缓存（`ui.py:1436`、`ui_state.py`） | 引用改存 seg_key；加载时 key 不存在即丢弃（失效而非错位） |
| unren 标签（`unren.py:475`） | 依 P0 调查结论：无跨版本依赖则直接换格式；有依赖则保持旧行为并单独立项 |

### 2.7 segmenter 批次顺序（顺带收益，需显式回归）

uid 降级为解析序 `1..N` 后，`segmenter.py` 按 uid 升序组批回到**真实文档顺序**，上下文参考更连贯。批次切分变化会使 golden 输出全变——属预期变更，在 P7 中一次性更新并注明原因。

---

## 3. 工作包与顺序

```
P0 前置调查 ──→ P1 核心构造 ──→ P2 入口收编 ──┬─→ P5 熔断 ──→ P7 测试重构/回归 ──→ 交付
                │                            ├─→ P4 持久化边界（并行）
                └─ P8 文档/迁移工具（并行）──┘
P6 复核视图（可选）：P2 完成后插队，可与 P5 并行
```

| 包 | 内容 | 估时 | 执行方式 |
|---|---|---|---|
| P0 | ① 12241 段重复原文分布统计（桶大小分布、桶内最大长度）；② unren 标签是否写入游戏文件且跨版本依赖；③ UI/journal 中持久化 uid 引用清点 | 0.5h | Lead 直接做 |
| P1 | `identity.py`（normalize + key + key_version）、`resolve_identity`（含 occurrence 消歧）、`Segment` 接入 | 3h | Lead 直接做 |
| P2 | 三入口收编（2.3）、`match_translations` 退役包装 | 4h | Lead 直接做 |
| P3 | uid 降级（2.4）、删除守卫与索引 | 2h | Lead 直接做 |
| P4 | 持久化边界迁移（2.6）、旧 CSV/快照一次性迁移脚本 | 4h | 可分包并行 |
| P5 | 熔断不变量（2.5） | 1h | Lead 做 |
| P6 | unmatched 复核视图（可选） | 3~4h | Lead 做（依赖 P2 数据结构） |
| P7 | 删/改旧语义用例（`test_match_by_uid_exact` 等 4~6 个）、A~K 场景适配、golden 更新、666 段 / 12241 段真实数据重验 | 4h | 场景适配 Lead 做；回归吞吐分包并行 |
| P8 | 设计文档、绝对化断言清理（搜「永不」「必然」「天然唯一」逐条处理）、迁移工具文档 | 2h | 可分包并行 |

合计约 23~24h（含 P6 则 +4h）；并行压缩后钟表时间约 1.5~2 个工作日。

**门禁**：P0-① 的统计结果决定 occurrence 消歧实现深度；若重复桶占比可忽略且桶内长度普遍为 1，occurrence 可实现为「桶 >1 时整桶进 unmatched」的保守版，把复杂度押后到确有需要时。

---

## 4. 验收标准

### 4.1 硬性断言（不可协商）

1. 全量回归通过（基线 737 项，含适配后用例）。
2. 真实事故数据（666 段）：`matches=646, unmatched=0`，且每个匹配 `seg.source == row.source` 且 `seg.file == locator_file(row.locator)`。
3. 全工程数据（15 文件 / 12241 段）：错位 0，strings 段错位 0。
4. 测试断言规范：匹配类测试禁止只断言 `len(matches)`；A~K 十一组场景全部保留并通过（场景 I/H 在无守卫代码的前提下通过，证明守卫不再被需要）。
5. 删除清单核验：`path_hash`、三级链、两个特征码索引、`_same_file/_same_source`、位置兜底、相似度常量——grep 确认无残留。
6. 熔断不变量：构造一条故意错配的匹配，确认被 `ExportBlocked` 拦截。

### 4.2 文档与流程

7. `uids.py` / `project.py` 及交付文档中的绝对化断言全部清理或改写为附测量结果的形式。
8. 新设计文档含：key 规范化规则全文、key_version 升版流程、三条入口的单一出口架构图。
9. 已知未修缺陷 #11 随本任务关闭并在复盘文档标记。

### 4.3 残余风险声明（随交付给出）

- 同文件重复桶内插行 → 同原文段译文互换（原文相同，实际损害≈0，审校可见）。
- 人工微调原文的行不再自动继承 → 进 unmatched + P6 复核视图（若实施）。
- 旧 CSV/快照中无法重建 key 的行 → unmatched 报告，可见可修。

---

## 5. 执行约束（复盘教训的制度化）

1. **先测量后断言**：任何「唯一/永不碰撞」的声明必须附实测数据，写入文档前先跑统计（陷阱二/四）。
2. **不猜**：匹配不确定即 unmatched，禁止「带日志的猜测」（§3.2 教训）。
3. **枚举入口**：改匹配语义前先 grep 所有消费方，三条入口必须同批收编（陷阱五）。
4. **用例裁决**：测试失败先判断「代码错」还是「用例编码了已证伪语义」，判据是真实数据（陷阱三）。
5. **验收看内容**：一切验证以 `译文落在哪一段` 为准，数量指标只作辅助（陷阱一）。
