# BUGFIX(ctx-uid-collision)：前文参考区编号与待翻译 uid 撞车导致译文错位

- 日期：2026-10-24
- 触发：用户实测反馈（导入1~2文件→翻译→写回→再导入新文件→翻译，
  预览框出现错位译文）
- 状态：✅ 已修复（429 全量用例通过，新增 1 用例 + 3 处断言更新）

---

## 1. 现象

按特定路径操作后，翻译页实时预览出现「原文与译文张冠李戴」：
某行的译文明显属于另一行（如 `Ask Kara for help...` 显示
`再和她做一次`——那是下一行 `Do it with her again` 的译文）。

## 2. 定位

多轮排查后锁定 prompt 层的**编号撞车**（数据层 uid 唯一性、
context 传递、provider 对位、引擎应用全部验证无误）：

1. `segmenter.chunk` 产出 **2 元组** context `(source, target)`；
2. `provider._normalize_context` 对二元组按 `enumerate` **位置补号**：
   第 0、1、2 条 → uid 记为 0、1、2；
3. `build_user_prompt` 把该编号当 uid 展示：`前文参考：0|Old one / 1|Old two`；
4. **触发条件**：首次导入时 uid 是大数字（765+），与 context 的 0,1,2
   不重叠，模型不混淆；**写回后再次导入新文件**时
   `build_project` 把 uid 重编为 1..N，与 context 的位置编号
   0,1,2... **大面积重叠**——同一个编号（如 1）同时出现在
   「前文参考」和「待翻译」两个语义区；
5. 模型看到重复编号，输出的 `{"i":1,...}` 指错行 → 译文错位。

复现验证：`context=[(0,"Old one","旧译1"),(1,"Old two","旧译2")]`
+ `待翻译=[(1,"New one"),(2,"New two")]` 时，编号 1 跨区重复
（重叠比例 1/2；context 3 条时 3/3 全重叠）。

## 3. 修复

1. **context 行去掉数字编号**，改为引号包裹的纯文本：
   `"原文"\n  → "译文"`——从根上消除与 uid 撞车的可能；
2. context 区标题加粗提示：**「前文参考（仅供语气衔接，不要翻译）」**；
3. 系统提示词补充说明两个区的分工：前文参考不是待翻译内容，
   也不要为它输出任何 JSON。

## 4. 测试

- 新增 `test_user_prompt_context_no_numbered_collision`：context 区
  不得出现任何「数字|」形态行（正则断言），待翻译区编号保留；
- 更新 `test_user_prompt_context_pairs` / `test_provider_passes_context_and_terms`
  / `test_user_prompt_context_capped_at_three` 的断言（新格式）；
- 回归：**429 passed**。

## 5. 影响面

- 仅提示词文本层：解析、护栏、引擎应用、写回全部不变；
- 模型侧更难混淆（编号唯一 + 显式区分两区）——对已观察到的
  「模型错编号」类错位是直接修复；
- 历史工程无需处理；重翻受影响批次即可。
