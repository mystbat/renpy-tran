# BUGFIX(unrpyc-translated)：反编译已译文件的中文台词不再进入待翻队列

- 日期：2026-10-20
- 触发：用户实测反馈（审校页大量 `[Pending]中文`；project.json 中
  646 条 pending 段的 source 全是中文台词）
- 状态：✅ 已修复（372 全量用例通过，新增 1 用例 + 1 处断言修正）
- 调研：先复盘 2.0（archive/package/2.0）的处理方式，再定 3.0 方案。

---

## 1. 现象

导入 unrpyc 反编译的已汉化游戏翻译文件（如 `tl/chinese/exp-1-3somes.rpy`）
后，审校页出现大量 `[Pending] 我们径直走到那里；卡拉不断地问我问题……`
——**中文台词被立为待翻段**，送翻 = 中文→中文，纯浪费 token。

## 2. 2.0 版如何处理（调研结论）

2.0 的输入是 **Ren'Py 官方生成的翻译模板**（`rpybak/*.rpy`）：每个
`translate chinese <id>:` 块都是「`# "英文原文"` 注释行 + 英文台词行」。

- **原文永远来自 `#` 注释行**（tocsv.py 存为「原文注释」列，torpy.py
  写回时按注释行定位）——原文来源与台词行内容无关，因此 2.0 **不需要**
  任何语言检测；
- 台词行初始全是英文，不存在「中文当原文」的形态；
- 唯一相关逻辑是事后质检：`check-csv.py` 的 `contains_english()`
  （`[a-zA-Z]` 正则、忽略转义符）检查译文残留英文——是质检不是导入过滤。

结论：2.0 未覆盖「unrpyc 反编译已译文件」这种输入形态（其流程起点是
官方模板）；3.0 的解析器把它当普通待译块导入，是新形态下的缺口。

## 3. 修复方案（对齐 2.0「原文必须来自可靠来源」的语义）

`_handle_translate` 的 dialogue 台词行立段改为 `_emit_dialogue`，三形态：

| 形态 | 判据 | source | target | status |
|---|---|---|---|---|
| 官方模板 | 块内有 `# "X"` 注释行（紧邻台词行） | X（注释行） | 台词文本 | 有文本 TRANSLATED / 空 PENDING |
| unrpyc 已译 | 无注释行 且 台词含 CJK | 台词文本 | 台词文本 | TRANSLATED |
| unrpyc 未译 | 无注释行 且 台词无 CJK | 台词文本 | 无 | PENDING |

- CJK 判据 `_has_cjk`：`\u2e80-\u9fff` + `\uf900-\ufaff` + 假名。
  全角标点不算——中文标点残留的未译行不会被误判为已译。
- 官方模板形态的 `#` 行以前被直接跳过（`comment` 字段只是台词的复读），
  现在记录 `pending_src_comment` 供台词行消费——**comment 字段语义变为
  「原文来源」**，与 2.0「原文注释」列对齐。
- regenerate 字节保留不受影响（回写按 locator 定位台词行替换引号内文本）。

## 4. 实测锚点

`exp-1-3somes.rpy`（674 段）：

- 修复前：pending 646 / translated 28（28 条是 old/new 对）；
- 修复后：translated 673 / pending 1（唯一 pending 的 source 为空串，
  是 `new` 形态边界，见 BUGFIX-old-new-pair §5b）。

## 5. 测试

新增 `test_dialogue_source_forms`：三形态逐一断言（source/target/status）。
修正 `test_project_store_roundtrip` 的 pending 断言（fixture 第 2 块
「你好，世界。」现在导入即 TRANSLATED，stats.pending 改按解析结果实算）。

回归：`python -m pytest tests -p no:cacheprovider` → **372 passed**。

## 5b. 追加修复：SDK 空模板不再误标 TRANSLATED（BUGFIX(sdk-template-pending)，2026-10-21）

用户用 Ren'Py SDK 重新生成翻译模板（`# "英文原文"` 注释行 + **英文**
台词行，尚未翻译）后导入，103 条被误标 TRANSLATED——形态 1 原判定
「有注释行 + 台词非空即已译」，但空模板的台词行内容就是英文原文本身。

排除清空工程因素：清空逻辑无状态残留，project.json 数据直接证明是
解析判定问题（translated 段的 target 全部等于 source）。

修复：形态 1 判定改为 **「台词与注释原文不同才算 TRANSLATED」**；
相同 = SDK 空模板未翻译 → PENDING、target 置空。四形态语义：

| 形态 | 判据 | source | target | status |
|---|---|---|---|---|
| 1a 模板已译 | `#` 注释行 + 台词≠原文 | 注释原文 | 台词 | TRANSLATED |
| 1b 模板未译（SDK 空模板） | `#` 注释行 + 台词=原文 | 注释原文 | 无 | PENDING |
| 2 unrpyc 已译 | 无注释 + CJK 台词 | 台词 | 台词 | TRANSLATED |
| 3 unrpyc 未译 | 无注释 + 非CJK台词 | 台词 | 无 | PENDING |

实测：SDK 模板文件 646 段从 pending 543 / translated 103 →
**pending 619 / translated 27**（27 条均为合法的 old/new strings 对，
其原文在 old 行，不受本判定影响）。

测试：`test_dialogue_source_forms` 扩为四形态断言（含 1b 回归守卫）。
回归：**377 passed**。

## 5c. 追加修复：old/new 对的 new=old 同样是空模板占位（2026-10-21 续）

用户复核：修复 5b 后仍有 27 条 TRANSLATED，全部是 `old/new` strings 对
——SDK 空模板里 `new` 行内容 = `old` 原文（未翻译占位），`_emit_old_new_pair`
的「new 非空即已译」判定与 5b 的 dialogue 形态 1 犯了同样的错。

修复：`_emit_old_new_pair` 判定改为 **「new 与 old 原文不同且非空才算
TRANSLATED」**；相同 = 空模板占位 → PENDING。孤立 new（source=""，
用户裁决形态）不受影响（source 空 → target≠source 恒成立）。

实测：SDK 模板文件最终 **646 段全部 PENDING**（纯未翻译模板的正确状态）。

测试：新增 `test_old_new_same_text_is_pending`（1b 的 strings 版回归
守卫）。回归：**378 passed**。

## 6. 用户操作指引

需**重新导入**该游戏文件：旧 project.json 中 646 条假 pending 段将被
替换为 673 条 TRANSLATED（译文已在段内），不会消耗翻译额度；剩余真正
待译的英文行正常送翻。写回时全部按 locator 原位替换，字节级无损。

## 7. 影响面

- 解析层：`comment` 字段在 dialogue 形态下从「台词复读」变为「原文
  来源」（官方模板形态）或不变（unrpyc 形态）；`source` 在官方模板
  形态下从台词文本变为注释行原文。依赖 `comment` 做展示的 UI（审校页
  定位符/注释展示）显示的将是真正的原文，语义更正确。
- 术语抽取（terms.py 信号源 A/B）基于 kind/source，形态 1/2 的段
  status=TRANSLATED 后不进抽取队列，与「已译不重抽」语义一致。
- 2.0 迁移（migrate.py `_align_dialogue`）：rpybak 官方模板的注释行
  现在会成为 source，feature+行序对齐逻辑不变；译文回填 target 不受
  source 变化影响。全量测试回归通过。
