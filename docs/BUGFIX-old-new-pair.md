# BUGFIX(old-new-pair)：`old`/`new` 成对解析——译文不再被当作待翻原文

- 日期：2026-10-20
- 触发：用户实测反馈（翻译页「最新译文」实时预览错乱）
- 状态：✅ 已修复（354 全量用例回归通过，新增 2 用例）

---

## 1. 现象

用户导入 Ren'Py 翻译文件 `tl/chinese/data.rpy`（unrpyc 反编译产物，strings 块
为成对 `old "英文"` / `new "中文"`）并执行翻译后，翻译页实时预览显示：

```
[1] OSWALD'S SWORD
    → 奥斯瓦尔德的剑        ← 正确

[1] 奥斯瓦尔德的剑           ← 上一句的中文译文，却被当作原文
    → 皮甲                  ← 不知来源的中文
```

自第二行起全部错位，译文内容与原文对不上。

## 2. 定位

**导入（解析器）bug，非显示 bug。** 排查链：

1. 预览面板 `_append_preview`（pytran/ui.py）按 `seg.uid` 回查
   `project.segments`，如实显示段落内容——显示层无辜。
2. 查 `project.json`：uid1 = `old "OSWALD'S SWORD"`，**uid2 =
   `new "奥斯瓦尔德的剑"`**——中文译文被立成了独立的待翻段落。
3. 根因：`pytran/parser.py` 的 `_OLD_NEW_PAIR_RE` 同时匹配 `old`/`new`
   行，`_handle_strings` / `_handle_translate` 对每行**各自独立**调用
   `_emit`。于是每对 `old/new` 拆成两条段，第二条把中文当原文送翻——
   预览里的「皮甲」正是对 `new "皮甲"` 这条假原文再翻译（或护栏兜底）的产物。

## 3. 修复方案

`old`/`new` 一对只立**一条**段：

| 场景 | 行为 |
|---|---|
| `old "X"` 后紧跟同级 `new "Y"`（允许空行/注释间隔） | 一条段：source=X，target=Y；Y 非空 → `TRANSLATED`（已有反编译/人工译文，不再机翻），Y 空 → `PENDING` |
| `old "X"` 无配对 `new` | 旧行为：立一条 PENDING 段 |
| 孤立 `new "Y"`（无 old） | 不立段（防止译文再度混入待翻队列） |
| locator | 指向 `new` 行——回写时替换的是 new 的引号内容，符合 Ren'Py 语义 |
| legacy `old:`/`new:` 子块（分号头形式） | 同样成对：`old:` 体为原文、其后 `new:` 体为译文；孤立 new 体跳过 |

涉及的代码路径：

- `_handle_strings`：`old/new` 单行对 → `_peek_new_pair` + `_emit_old_new_pair`
- `_handle_translate`（`translate <lang> strings_<hash>:` 头内的 old/new）：同上
- `_handle_legacy_old_new_body`：分号子块体配对（`_body_owner` 判定体属于
  `old:` 还是 `new:`）
- `_emit` 新增 `target` 参数

## 4. 测试

新增（tests/test_parser_roundtrip.py）：

- `test_old_new_pair_not_double_counted`：两对 old/new → 恰好 2 条段
  （旧实现 4 条），status=TRANSLATED，target 为中文译文，locator 指向 new 行。
- `test_old_new_empty_new_is_pending`：`new ""` → PENDING 可正常送翻。
- `test_parse_emits_only_translatable_or_skipped` 扩展：允许 TRANSLATED
  状态（带既有译文的段）。

回归：`python -m pytest tests -q` → **354 passed**（0 failed）。

真实文件实测锚点：`data.rpy` 5 对 old/new → 5 条段，原文↔译文一一对应
（OSWALD'S SWORD→奥斯瓦尔德的剑、LEATHER ARMOR→皮甲、Drunk goblin→醉鬼、
drunk→醉、Rusty small axe→生锈的小斧头）。

## 5. 用户操作指引

已导入的旧项目 `project.json` 含错误段（中文被当原文），需**重新导入**
该游戏文件后再翻译。带既有译文的段落导入即 `TRANSLATED`，不会重复消耗
翻译额度；如需重译可到审校页对该行操作。

## 6. 影响面

- 解析层行为变更：`old/new` 成对文件的段数减半，uid 编号相应变化。
- `migrate.py`（2.0 迁移按块序+行序对齐）与术语抽取依赖段列表的模块
  已由全量测试回归覆盖，无破坏。
- 字节级往返（regenerate）不受影响：locator 指向 new 行，未译/已译的
  回写均只替换 new 引号内文本（roundtrip 测试全绿验证）。
