# BUGFIX(layer0-scavenger)：逐对象打捞层——悬空逗号/截断响应不再整批失败

- 日期：2026-10-20
- 触发：用户实测反馈（第三例，同一形态复发：`响应解析失败 uid=1..20`，
  响应原文为无括号多行对象流）
- 状态：✅ 已修复（371 全量用例通过，新增 5 用例）
- 关联：`docs/BUGFIX-layer0-fence.md`（第二例，栅栏）；layer0 首例见
  commit `1492040`。

---

## 1. 现象

与第二例相同的报错：`ProtocolError: 响应中没有任何可解析的编号行`，
响应原文前 200 字符为无括号多行对象流（形态与首例修复目标一致）。
栅栏修复后本地按「完整对象流」复现不出了——问题在响应**不完整**。

## 2. 定位

第三例失败形态（本地逐一复现）：

| 形态 | strict json | 逐行层 | 结果 |
|---|---|---|---|
| 对象流 + **末尾悬空逗号**（`...\n},\n`） | ✗ | 碎行全灭 | 整批失败 |
| 响应**被截断**（max_tokens，最后对象不完整） | ✗ | 碎行全灭 | 整批失败 |
| 对象流前后附**闲话** | ✗ | 碎行全灭 | 整批失败 |

根因：`_parse_json_array` 的兜底分支要求 `endswith("}")` 后整体
`json.loads`——以上三种形态都不是合法 JSON，strict 解析必败；逐行层
只能救「单行内完整对象」，多行展开的对象流碎行无结构可解析。

## 3. 修复方案：新增打捞层（scavenger）

解析层级现在是（`pytran/protocol.py`）：

```
第 0 层  _parse_json_array：整体 JSON（含栅栏剥离、无括号包裹、
         悬空逗号清理）——只做「能整体成功」的解析
逐行层   _parse_one_line × _strip_fence_lines：单行 JSONL / 退化
         「编号|文本」——保住 unparsed 诊断记录
打捞层   _scavenge_objects：扫描平衡的 {...} 片段（引号/转义感知）
         逐个 json.loads，失败的跳过——仅前两层全灭时调用
```

打捞层设计要点：

- **顺序**：放在逐行层之后。若放在第 0 层内，会把「单行坏 JSON 流」
  一类形态提前救回，坏行不再进 `unparsed`，丢失诊断信息（测试
  `test_parse_broken_json_line_is_unparsed` 捕捉到了这个回归）。
- **括号扫描**：引号内字符与反斜杠转义不计入括号深度；未闭合对象
  （截断尾）即停——后面不会有更多完整对象。
- **部分成功**：截断响应中已完整的前 N 行全部回收，截断行进
  `missing`，由引擎 §5.3 定点重发，不浪费已翻内容。
- **失败语义不变**：纯闲话/完全无对象仍抛 `ProtocolError`。

## 4. 测试

新增（tests/test_protocol_layer0.py）：

- `test_trailing_dangling_comma`：末尾悬空逗号 → 整批回收；
- `test_truncated_mid_object`：截断 → {1,2,3} 回收 + 4 进 missing；
- `test_fenced_truncated`：围栏 + 截断组合；
- `test_prose_wrapped_multiline_objects`：闲话包夹多行对象流；
- `test_garbage_still_raises`：纯闲话仍抛（失败语义守卫）。

关键回归守卫（既有用例，验证层级顺序未破坏诊断语义）：

- `test_parse_broken_json_line_is_unparsed`（坏行仍进 unparsed）
- `test_parse_skips_prose_lines`（闲话行计数保留）

回归：`python -m pytest tests -p no:cacheprovider` → **371 passed**。

## 5. 用户操作指引

重新翻译该批 FAILED 行即可。若响应是被截断的，已回收行不会重翻，
只有截断行会重发；若截断频繁出现（日志反复出现同一 uid），建议在
配置中降低块大小（batch lines）或提高 max_tokens。

## 6. 追加修复：合并大对象的键值对打捞（BUGFIX(pairs-scavenger)，2026-10-21 第四例）

**现象**：`响应解析失败 uid=21..40`。响应形态（新）：整批被模型输出为
**单个大 JSON 对象** `{"i":21,...,"i":22,...}`（键重复），且 uid24 的
t 值是未加引号的裸文本（`*咽口水*` 后引号丢失）。

**两条死路**：

1. `json.loads` 对重复键**后者覆盖前者**——"成功"解析却只剩最后一条；
2. uid24 裸文本使整体 strict 解析必败——打捞层把整个 `{...}` 当一个
   对象，解析失败整体跳过。

**修复**：

- `_scavenge_pairs`：对象解析失败时，在片段内以 `"i": <数字>` 为锚
  切分记录、`"t"` 取带引号串逐条回收；残缺条（裸文本/缺 t）跳过，
  编号进 missing 由引擎定点重发；
- 第 0 层检测合并大对象形态（多 `"i"` 锚 **且** 无对象边界 `}{`）时
  直接键值对切分，避开键覆盖；
- 边界判据 `\}\s*,?\s*\{`（逗号可有可无——JSONL 对象间无逗号）+
  引号内文本归零防误判。JSONL（携带 k/r 申报字段）因此正确留在
  逐行层，guardrail 4 个申报用例作回归守卫。

**测试**：`test_merged_object_with_bare_value`（残缺条进 missing）、
`test_merged_object_fully_valid`（全完好全回收）。回归：**380 passed**。
