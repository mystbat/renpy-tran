# W5d 交付说明（翻译选项卡问题清单修复）

- 任务编号：W5d（W5 系列延续）
- 任务来源：`docs/翻译选项卡问题清单.md`（对照设计文档审查翻译选项卡后产出的问题清单，共 24 项）
- 用户裁决（2026-10-19）：按 P0→P1→P2 分期批准执行；A5 改文档不改代码；B7/B9/C7/C8 仅记录
- 设计依据：§4.1 / §4.3 / §5.3 / §5.4 / §6.5 / §7 / §8.1 / §8.4 / §8.5 / §9 / §15.3
- 分支模式：`.worktrees/W5` + `task/W5`（W5 系列延续）；P0 两项由总控直接在主树修（改动 <10 行）
- 测试结果：**280 passed / 0 failures / 0 errors**（基线 241 → 新增 39）

---

## 一、本次做了什么

按问题清单的分期落地。**P0 止血 → P1 翻译页补齐 §9 → P2 工具区/术语表/文档**。

### P0 —— 止血 + 补测试闸门（4 项）

| # | 问题 | 修复 |
|---|---|---|
| 1 | **A1** 设置对话框「保存」在术语表存在「原文非空+译名为空」行时必崩（`re` 未导入） | `ui.py` 补 `import re` |
| 2 | **A2** `timeout`/`max_retry_rounds`/`context_lines`/`block_lines` 四个数值字段在设置里退化为字符串 | `_coerce_setting` 类型表扩到 12 个数值字段；新增模块级 `_NUMERIC_ATTRS` 作为单一真相源 |
| 3 | **A3** `save()` 直接 `setattr` + `cfg.save()`，绕过 `_apply_settings`（测试覆盖的路 GUI 不走） | `save()` 改走 `_apply_settings(values)`；设置页补 `block_lines` 字段 |
| 4 | **B5** 保存弹窗仍称「API Key 不会写入文件」，与 ADR-0004 修订及 Tab1 标签自相矛盾 | 弹窗文案改为「写入本机 config.ini（已 gitignore）+ 环境变量优先」 |

> 用户特别指出 P0 第 4 项（GUI 级回归测试）「与 P1 并列——它是防止复发的关键闸」，
> 故以最高优先级落地，见 §3。

### P1 —— 翻译页补齐 §9（6 项）

| # | 问题 | 修复 |
|---|---|---|
| 5 | **C1** `round` 事件把进度条打成 indeterminate 后，`_refresh_progress` 从不恢复 `setRange(0,100)`，百分比永久失效 | 新增 `_set_translate_busy()`：busy 时才切 `(0,0)`，恢复时显式 `setRange(0,100)` |
| 6 | **C2** 空工程被 `total or 1` 掩饰成 0/1 | 空工程显示 0/0 |
| 7 | **A6** 翻译页无 §15.3 成本闸/预算展示 | 新增 `translate_info` 行（模型/并发/温度/块大小/前文 + 待译行数/估算 token）、`budget_warn` 警告条、「设置预算上限…」按钮、`_confirm_budget` 闸 |
| 8 | **B1** 「重试失败项」与「开始自动翻译」共用同一回调 | `_retry_failed()` 只传 `only_uids={failed uids}`；无失败项时给提示不空跑 |
| 9 | **C3/C4** worker 线程直接 `state.goto(3)` 违反 §9「后台线程只发事件」 | 改发 `goto_step` 事件由主线程消费（含越界保护） |
| 10 | **C6** 翻译期间无取消按钮 | 「■ 停止」接入 `engine.stop()`；翻译中/空闲两态切换按钮可用性 |

### P2 —— 工具区、术语表与文档（6 项）

| # | 问题 | 修复 |
|---|---|---|
| 11 | **A4** §9 工具区「辅助补丁 + 移除补丁」整块缺失（`unren.apply_patch/remove_patches` 早已实现但无 UI 入口） | 选择目录页新增 `QGroupBox`：4 个勾选框（默认不勾）+「注入补丁」「移除补丁」+ 状态标签；按 `game_root` 置灰；复用 unren 的幂等实现 |
| 12 | **B3** 「从工程抽取候选」直接灌表，第二列写死 `(出现 N 次)` 伪译名（用户直接保存会写进 `terms`） | 新增 `_pick_term_candidates()` 候选对话框（候选词+次数+译名输入+删除行）；工程为空时按钮置灰 |
| 13 | **B4** 术语表页缺「命中术语的英文不判为含英文（白名单）」复选框 | 加 `QCheckBox`，控制豁免输入框是否生效 |
| 14 | **B6** 缺「重复原文合并提示 / 保存结果汇总」 | 保存弹窗汇总术语/豁免条数 + 重复原文已合并提示（复用 `terms.detect_duplicate_originals` / `rows_to_terms`） |
| 15 | **B8** `profile.json` 损坏时异常冒到 GUI 外层 | `_load_profile` 加 try/except：按空 profile + warn 告警 |
| 16 | **A5** §9「选项卡不可用置灰」被放弃，属未经裁决的设计偏离 | **改文档**：设计文档 §9 修订（自由导航、进度条含停止按钮、翻译页参数/成本行、设置项补术语表/切块/前文）+ 新增 `docs/adr/ADR-0011-选项卡自由导航放弃顶部步骤置灰.md` 并登记索引 |

---

## 二、`9ee271f`「翻译环节五项 UX 修复」的三处断线（用户反馈有 bug）

该提交给 config/engine/Segmenter 接了能力，但 **UI 侧三项没接**，属于「引擎发了、UI 不收」：

| 声称做了 | 实际状态 | 本次修复 |
|---|---|---|
| ① 切块大小可调 `config.block_lines` | config/engine/Segmenter 都接了，**设置页漏了字段** → GUI 里无法配置 | 设置 Tab1 补「切块大小 (block_lines，行/块)」字段 |
| ③ 块级反馈 `chunk_done` 事件 | 引擎每完成一块发一次，**`_apply_event` 没有对应分支** → 进度从不按块前进 | 加 `chunk_done` 分支：按块数走百分比 + 文字「翻译中：块 N/M」 |
| ④ 停止按钮 `engine.stop()` | 引擎实现完毕，**UI 无「停止」按钮** | 翻译页加「■ 停止」，接入 `engine.stop()`，busy 态联动 |

> ②（`context_lines` 设置字段）在 `44bbf65` 已补；⑤（多选导入）已正确落地。

---

## 三、测试执行结果

### 3.1 新增测试（4 个文件 / 39 例）

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_ui_settings_dialog.py` | 7 | **A1**：术语表「原文有值/译名为空」行点保存不抛 `NameError`（含纯译名对照组）；**A2**：四个数值字段保存后**不重启也是 int/float**、`effective_max_retry_rounds()` 不 `TypeError`、清空保留既有值；**A3**：`save()` 必须走 `_apply_settings`；**B5**：弹窗文案与 ADR-0004 一致；`block_lines` 字段在位 |
| `tests/test_ui_translate_page.py` | 16 | **C1**：`round` 后 `progress` 事件必须恢复确定性模式、`chunk_done` 驱动百分比；**C2**：空工程 0/0；`block_lines` 字段；停止按钮存在/态切换/调 `engine.stop()`/无 engine 安全；**B1**：`_retry_failed` 只传 FAILED uid、无失败项不启动；**A6**：预算闸三态（拒绝/确认/通过）+ 警告条显隐 + 参数行内容；**C3/C4**：`goto_step` 事件切页、越界拒绝、worker 只发事件不写 state |
| `tests/test_ui_terms_page.py` | 6 | **B3**：候选对话框只并入「填了译名」或「原文含英文」的条目、取消返回空、工程为空置灰；**B4**：白名单复选框控制豁免输入框生效与否；**B6**：保存弹窗汇总+重复原文合并提示+后值覆盖；**B8**：损坏 profile → 空 profile + warn |
| `tests/test_ui_patches.py` | 10 | **A4**：4 个勾选框存在且默认不勾、无 `game_root` 置灰、只写勾中的补丁、已存在拒绝覆盖（幂等）、移除清空、重复移除幂等、未勾选不写、已注入的显示为勾选 |

### 3.2 测试方法说明（关键）

GUI 测试的做法是**构建真实窗口 + 驱动真实控件**，而非只测抽离的纯逻辑：

```python
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)   # run() 不阻塞
monkeypatch.setattr(QDialog, "exec", lambda self: 0, raising=False)        # 设置对话框不阻塞
monkeypatch.setattr(QMessageBox, "information", ...)                       # 弹窗不阻塞
```

三个必须 patch 的阻塞点（都是落地时实际踩到的坑，已在测试里注释说明）：
1. `run()` 末尾的 `app.exec()`；
2. 设置对话框的 `dlg.exec()`（模态）；
3. `save()` 末尾的 `QMessageBox.information`（模态）——**不 patch 会永久挂住**。

为支撑这种驱动，`ui.py` 在 `_open_settings` 末尾记录了 `self._settings_dialog = dlg`
（真实 GUI 不使用，仅供无头测试）。每个测试用独立 `tmp_path` 工作目录，
避免设置写入互相污染。

### 3.3 回归结果

```
# 新增四个测试文件
E:\Gameditor\py-tran\.venv\Scripts\python.exe -m pytest tests/test_ui_settings_dialog.py tests/test_ui_translate_page.py tests/test_ui_terms_page.py tests/test_ui_patches.py -p no:cacheprovider -q
→ 39 passed

# 全量回归（排除沙箱权限受限的 subprocess 真实 unrpyc 探测用例）
E:\Gameditor\py-tran\.venv\Scripts\python.exe -m pytest --ignore=tests/test_w4b_real_unrpyc.py -p no:cacheprovider -q
→ 280 passed in 23.14s   （基线 241 → 新增 39）
```

---

## 四、落地过程中发现并修掉的隐藏缺陷

以下三项不在原问题清单内，是实现过程中暴露的：

1. **`_open_settings` 在未配置 API Key 时打不开**
   `_open_settings` 用 `_build_default_deps()` 构造 provider，缺 Key 直接抛 `OpenAIError`。
   这正是 `cli.cmd_ui` 用 `allow_missing_provider=True` 的原因，但 UI 侧漏了这层保护。
   **修复**：新增 `_current_config()`，内部走 `build_deps(allow_missing_provider=True)`。

2. **`_on_apply_patches` / `_on_remove_patches` 同样会因缺 Key 抛错**
   补丁功能只需要 `unren`，不需要 provider。
   **修复**：两处改用 `_build_default_deps(allow_missing_provider=True)`。

3. **`_set_translate_busy` 的守卫条件**
   Qt 的 indeterminate 模式是 `setRange(0, 0)`，此时 `setValue()` 无效。
   若不做「已是目标态就跳过」的判断，`round` 事件高频到达时会反复 `setRange`。
   **修复**：`_set_translate_busy` 先比较 `maximum()` 再决定是否切换。

---

## 五、设计章节覆盖表

| 章节 | 要求 | 覆盖落点 | 测试 |
|---|---|---|---|
| §9 进度条 | `████████░░ 812/1000 失败 3` + 重试入口 | `_refresh_progress` + `_set_translate_busy` + `chunk_done` 分支 | test_ui_translate_page:2 项 |
| §9 停止 | 翻译中可中止，已译结果不丢 | `btn_stop` + `engine.stop()` | test_ui_translate_page:3 项 |
| §9 设置项齐全 | 含切块大小/前文行数/术语表/提示词 | `_open_settings` fields（14 项） | test_ui_settings_dialog:2 项 |
| §9 工具区 | `[辅助补丁□…] [移除补丁]`，默认不启用、可卸载、记日志 | `_on_apply_patches`/`_on_remove_patches`/`_refresh_patch_ui` | test_ui_patches:10 项 |
| §9 自由导航 | 五个选项卡不置灰，置灰收敛到页内按钮 | `_refresh_steps`（原实现）+ 设计文档 §9 修订 + ADR-0011 | 文档 |
| §15.3 成本闸 | 开始前估算，超预算拒绝/确认 | `_estimate_tokens` + `_confirm_budget` + `budget_warn` | test_ui_translate_page:3 项 |
| §6.5 失败重译 | 只重发失败行 | `_retry_failed(only_uids=…)` | test_ui_translate_page:2 项 |
| §7 术语表 | 抽取候选对话框、白名单、保存汇总 | `_pick_term_candidates` + 白名单 checkbox + 保存汇总 | test_ui_terms_page:6 项 |
| §8.1/§8.4 辅助补丁 | 四补丁、默认不启用、幂等、可卸载 | `unren.apply_patch/remove_patches`（既有）+ UI 接线 | test_ui_patches:10 项 |

---

## 六、ADR 与设计文档变更（A5）

**用户裁决：改文档，不改代码。**

1. **新增 `docs/adr/ADR-0011-选项卡自由导航放弃顶部步骤置灰.md`**
   记录「顶部五个选项卡永不置灰、置灰收敛到页内按钮」这一偏离，
   含背景（原偏离只写在 UI 交付说明里、未回写设计文档也未记 ADR）、
   决策、理由（查看不需前置条件、只有执行操作才需要；十万级工程下查看各页不受阻；
   用户实测清单第 10 项已按此验收）、后果（含「`control_enabled("review")` 目前未接线」的说明）。

2. **修订 `docs/3.0-重构设计文档.md` §9**
   - 布局图：`← 不可用置灰` → `← 自由导航（ADR-0011）`；进度条行加 `[■停止]`
   - 「状态驱动置灰」段 → 「顶部五个选项卡自由导航」+ 页内置灰规则四条
   - 新增：翻译页生效参数与成本估算说明；停止按钮语义
   - 「设置」对话框字段补：术语表与提示词追加段（§7）、切块大小与前文参考行数（§4.1）

3. **`docs/adr/README.md`** 索引表补 ADR-0011 行。

---

## 七、仍未处理（用户标注「记录待查 / 待分析」）

| 编号 | 内容 | 下一步 |
|---|---|---|
| B7 | 多 SDK 候选目录 + 按引擎版本自动挑选 + 填错即时提示（§8.5） | 需 §8.5 完整实现，单独排期 |
| B9 / C9 | `project.stats` 内存不同步，导入页摘要显示过期计数 | 在 `UiState` 或 `_refresh_*` 里统一重算 |
| C7 | `UiState._review_index` 私有属性被 GUI 直读直写（`ui.py` 多处） | `UiState` 加公开 `set_index()` / `index` 后收口 |
| C8 | 日志面板 warn 之后新行可能继承红色 | `_log` 每条前显式 reset 颜色 |

---

## 八、用户实测清单

### 8.1 设置对话框（A1 / A2 / A3 / B5）

| 序号 | 操作 | 预期 |
|---|---|---|
| 1 | 设置 → 术语表 → 添加行 → 原文填 `Stalkfap`、**译名列留空** → 保存 | **不再崩溃**（A1 修复点，此前此处必崩 `NameError`）；弹窗汇总「术语 N 条，免翻短语 M 条」 |
| 2 | 设置 → 常规 → 改「请求超时秒」为 60、「重译轮上限」为 2、「切块大小」为 20 → 保存 → **不重启**直接点「开始自动翻译」 | 参数立即生效，不报 `TypeError`（A2 修复点：此前 `min("3", 5)` 崩） |
| 3 | 设置 → 常规 → 把「请求超时秒」清空 → 保存 | 保留原值 120，不被清成空串 |
| 4 | 观察保存弹窗 | 文案为「写入本机 config.ini（已 gitignore）+ 环境变量优先」，不再说「不会写入文件」 |
| 5 | 无 API Key 的全新工作目录启动 GUI → 点「⚙ 设置」 | **能打开设置对话框**（此前此处抛 `OpenAIError` 打不开） |

### 8.2 翻译页（C1 / A6 / B1 / C6）

| 序号 | 操作 | 预期 |
|---|---|---|
| 6 | 导入 → 「开始自动翻译」 | 顶部信息行显示模型/并发/温度/块大小/前文行数 + 「待译 N 行，估算约 X token」 |
| 7 | 点「设置预算上限…」填入一个比估算小的值 | 出现红色警告条「⚠ 估算 X token 超出预算上限 Y」；点开始翻译时弹确认框，选「否」不开跑 |
| 8 | 翻译过程中观察进度条 | **按块前进显示百分比**（此前进入第 2 轮后永远转圈、百分比失效）；完成后显示「完成 done/total 失败 N」 |
| 9 | 翻译中点「■ 停止」 | 日志「已请求停止翻译」；当前块跑完后不再发新块，已译结果保留，按钮态恢复 |
| 10 | 制造几个失败行后点「重试失败项」 | 只重发失败行；无失败项时弹「当前没有失败项需要重试」 |
| 11 | 翻译完成 | 自动切到审校页（此前 worker 直接改状态，现走事件） |

### 8.3 辅助补丁（A4）

| 序号 | 操作 | 预期 |
|---|---|---|
| 12 | 选择游戏目录 → 看「辅助补丁」区 | 4 个勾选框**默认全部不勾**；状态标签「尚未注入任何补丁」 |
| 13 | 纯文件模式（只选 .rpy） | 补丁区置灰，状态标签「（仅游戏目录模式可用）」 |
| 14 | 勾选「强制跳过未读文本」→「注入补丁」 | 游戏 `game/` 下出现 `unren-skip.rpy`；日志记录；未勾选的不写入 |
| 15 | 再次勾选同一项 →「注入补丁」 | 提示已存在、拒绝覆盖（幂等），文件内容不变 |
| 16 | 点「移除补丁」 | 全部 `unren-*.rpy` 被删除；状态标签复位 |
| 17 | 无补丁时再点「移除补丁」 | 提示「没有找到已注入的补丁」，不报错 |

### 8.4 术语表（B3 / B4 / B6）

| 序号 | 操作 | 预期 |
|---|---|---|
| 18 | 设置 → 术语表 →「从工程抽取候选…」 | 弹**候选对话框**：候选词 + 出现次数 + 译名输入列；不再直接把 `(出现 N 次)` 写进表格 |
| 19 | 候选对话框里只给 `Ian` 填「伊恩」，其余留空 → 确定 | 只有 `Ian` 并入表格；含英文且留空的（如 `Stalkfap`）按原文保留并入；纯中文留空的被忽略 |
| 20 | 候选对话框点「取消」 | 不并入任何候选 |
| 21 | 取消勾选「命中术语的英文不判为含英文」→ 在免翻框填 `ManualPhrase` → 保存 | `ManualPhrase` **不**进入豁免表；重新勾选再保存则进入 |
| 22 | 表格里放两行同一原文（不同译名）→ 保存 | 弹窗提示「重复原文已合并（后值覆盖）」；`profile.json` 里是后一个值 |
| 23 | 工程为空时打开设置 | 「从工程抽取候选…」按钮置灰 |

---

## 九、变更文件清单

```
M pytran/ui.py                                  (+585/-54)
M docs/3.0-重构设计文档.md                        (+12/-5)   §9 修订（A5）
M docs/adr/README.md                             (+2/-0)    ADR-0011 索引
M docs/翻译选项卡问题清单.md                       (各条目加状态徽标 + 记录用户裁决与执行结果)
A docs/adr/ADR-0011-选项卡自由导航放弃顶部步骤置灰.md
A tests/test_ui_settings_dialog.py                (7 例)
A tests/test_ui_translate_page.py                 (16 例)
A tests/test_ui_terms_page.py                     (6 例)
A tests/test_ui_patches.py                        (10 例)
```

> 冻结契约 `models.py` / `config.py` / `interfaces.py` **未修改**。

---

## 十、提交记录（待 git 提交时回填）

按项目提交信息规范（`前缀: 做了什么 [对应章节/问题编号]`），计划拆成下列提交：

```
task(W5d): 补 import re 修复设置保存崩 + 数值字段类型收敛 + save 改走 _apply_settings [问题清单 A1/A2/A3]
task(W5d): 保存弹窗文案对齐 ADR-0004 修订 + 新增 _NUMERIC_ATTRS 单一真相源 [问题清单 B5]
task(W5d): 新增 GUI 级设置对话框回归测试（7 例，offscreen 驱动真实窗口）[问题清单 A1/A2/A3 复发闸门]
task(W5d): 进度条重做——round 不再永久 indeterminate + 空工程 0/0 [§9，问题清单 C1/C2]
task(W5d): 接入 chunk_done 块级进度 + 补 block_lines 设置字段 + 停止按钮 [9ee271f 三处断线]
task(W5d): 翻译页参数与成本估算行 + 预算确认闸 [§15.3，问题清单 A6/C5]
task(W5d): 重试失败项分流 only_uids + worker 改发 goto_step 事件 [§6.5/§9，问题清单 B1/C3/C4]
task(W5d): 选择目录页辅助补丁工具区（4 勾选 + 注入/移除）[§8.1/§8.4，问题清单 A4]
task(W5d): 术语表候选对话框 + 白名单开关 + 保存汇总 + 损坏 profile 告警 [§7/W5b，问题清单 B3/B4/B6/B8]
task(W5d): 设计文档 §9 修订 + ADR-0011 选项卡自由导航 [问题清单 A5]
docs(W5d): 翻译选项卡问题清单记录用户裁决与执行结果
```

> 说明：本次改动目前**尚未 git 提交**，全部在主树工作区。按用户指定的分支模式，
> W5 系列改动应落在 `.worktrees/W5` + `task/W5`；P0 两项用户授权总控主树修。
> 提交前需确认归档方式（见 §十一）。

---

## 十一、待总控/用户确认

1. **归档方式**：改动目前全在主树工作区。是否移到 `.worktrees/W5`（`task/W5`）后提交？
   还是按近期 `task(总控)` 模式直接在主树提交？
2. **GUI 实测**：§8 清单共 23 项，需用户在真实环境执行后反馈。
   重点：8.1 序号 1（A1 修复点）、序号 2（A2 修复点）、8.2 序号 8（C1 进度条）、
   8.3 序号 14/15（A4 幂等）。
3. **B7/B9/C7/C8** 四项已记录待查，未在本次范围。

---

## 附：验证方法与证据

| 方法 | 结论 |
|---|---|
| `pytest --ignore=tests/test_w4b_real_unrpyc.py -q --junitxml` | tests=280 failures=0 errors=0 skipped=0，23.14s |
| AST 绑定分析（A1 定位） | `save()` 引用 `re`，但模块级/类级/`_open_settings` 级/builtins 四级均无绑定 |
| 函数体抽取执行（A1 复现） | 抽出 `ui.py` 原 `save()` 函数体 + stub Qt 对象后 exec → `NameError: name 're' is not defined` |
| 测试覆盖核查（A1 为何漏网） | `grep "_open_settings" tests/*.py` → 0 命中；`_open_settings` 在 `run()` 内且 `run()` 标 `pragma: no cover` |
| 行尾核查 | 设计文档 CRLF、`ui.py`/`adr/README.md` CRLF（`core.autocrlf=true`）均已恢复，diff 无行尾噪音 |
