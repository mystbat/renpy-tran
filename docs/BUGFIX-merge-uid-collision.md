# BUGFIX(merge-uid-collision)：uid 分配稳定化——合并导入按 uid 对位不再撞车

> **⚠️ 本文档已废止（SUPERSEDED, 2026-10-26）**
>
> 本文档第 3 节的方案（uid 稳定分配 + "不同文件 hash 不同永不撞车"）
> **已被证伪并整体退役**：16-bit 路径 hash 在 3000 个路径上实测 59 次
> 碰撞（生日界 ≈ 69），"永不撞车"是未测量即写入的错误断言（复盘
> 陷阱二/四）。uid 匹配方案历经六轮补丁后，最终由**段身份体系**
> （`pytran.identity`，键 = 完整文件路径 + 原文）整体替代——见
> `docs/TASK-segkey-identity-refactor.md`（v3.1）与
> `docs/POSTMORTEM-misplacement-six-rounds.md`。
> 本文档仅作历史记录保留，**其中的方案描述与断言不再反映当前实现**。

- 日期：2026-10-24（第二轮修订：按用户裁决改为 uid 稳定分配方案）
- 触发：用户实测（写回后点「合并导回」→ 再写回 → **15 个文件全部被破坏**）
- 状态：✅ 已修复（473 全量用例通过，新增 12 用例）
- 数据恢复：用户游戏 15 个 `.rpy` 已从 `bak/game/20261024-220747` 快照
  逐字节恢复（SHA256 校验一致），`.rpyc` 缓存已清除待 Ren'Py 重编译。

---

## 1. 事故形态

写回后 `common.rpy` 第 0 对：

```
old "Self-voicing disabled."
new "恐怕不容易。最直接的路线将会穿过森林。"   ← 完全无关句子的译文
```

| 文件 | 损坏 |
|---|---|
| common / data / exp-1-3somes / screens | 内容错位（译文配错原文） |
| exp-wos | 2322 对内容错位 |
| 其余 10 个 | 仅排版空行膨胀（内容正确） |

## 2. 根因

「合并导回」（`ui._merge_import`）走 `backfill.match_translations`，
第一级匹配是 **uid 精确**。而 `build_project` 每次导入都把 uid **从 1
重编**——uid 只是「单次导入内的段序号」，跨导入毫无语义：

```
旧工程 uid=5（common.rpy 第5段，译文「自动朗读已禁用。」）
   ↓ uid 精确命中
新工程 uid=5（完全不同的另一段）
   ↓ 旧译文写入 + REVIEWED
用户写回 → 15 个文件译文按错位 uid 配回文件 → 全面污染
```

## 3. 修复方案（用户裁决）：uid 分配稳定化，uid 保持唯一主键

用户指出：**uid 本来就是唯一性主键，不该改匹配逻辑；该修的是 uid 分配**。
按此思路实现（`pytran/uids.py` 新增）：

```
uid = 文件路径 hash(16bit) << 16 | 文件内序号(16bit)
```

* **同文件同一条段，任何次导入得到同一 uid**（文件内容不变时）——
  合并导入时 uid 直接对位，无需任何原文/特征码/相似度猜测；
* **不同文件路径 hash 不同，永不撞车**——多文件导入天然全局唯一；
* 新 uid 恒 ≥ 1<<16，与旧格式（1..N 顺序号）可区分。

配套改动：

| 位置 | 改动 |
|---|---|
| `pytran/uids.py`（新增） | `make_uid(path, seq)` / `assign_uids_flat(segments)` |
| `project.build_project` | 默认按稳定分配器重编；`reassign_uids=False` 可保留调用方 uid（CSV 回填同会话场景） |
| `project.JsonProjectStore.load` | 检测旧格式 uid（全 < 1<<16）→ 按 (文件, 出现顺序) 迁移为稳定 uid，内容不丢 |
| `backfill.match_translations` | `use_uid_matching` 默认 **True**（uid 已稳定，主键恢复）；不可信来源可显式关闭退特征码兜底 |
| `ui._merge_import` | 回归纯 uid 对位：旧 uid 命中 → 保留 target + REVIEWED；未命中 → 记「遗留译文」日志；日志区分「uid 对位 / 兜底命中 / 遗留」 |

## 4. 测试

- 新增 `tests/test_stable_uid.py`（11 用例）：跨调用稳定 / 跨文件唯一 /
  文件内有序 / 新旧格式可区分 / build_project 两次导入 uid 一致 /
  多文件无碰撞 / 保留顺序 / opt-out / **合并导入 uid 对位核心回归** /
  跨文件不泄漏 / 旧工程加载迁移；
- 新增 `tests/test_backfill_merge_uid.py`（5 用例）：uid 匹配默认开启 /
  稳定分配杜绝撞车 / 可关闭退特征码 / 特征码不受影响 / 单段单译；
- 更新 `test_backfill.py` / `test_parser_roundtrip.py` 旧断言；
- 回归：**473 passed**。

## 5. 用户操作

无需改变习惯——「合并导回」现在按 uid 精确对位，安全可靠。
旧工程（顺序 uid）在下次打开时自动迁移为稳定 uid，与后续导入对齐。

## 6. 设计说明（为何不用原文匹配）

原文/特征码/相似度匹配在重复台词多的游戏里天然不可靠（同原文多处
出现、短句相似度高），而 uid 稳定分配是确定性的一一映射——这正是
用户裁决的方案，也恢复了「uid 即主键」的简单心智模型。
