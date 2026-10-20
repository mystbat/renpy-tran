# W5c 补充任务：术语候选抽取算法重做（当前实现产出无效）

> 任务来源：总控审核 + 用户实测。用户原话：「从工程抽取候选依然是一大堆无效内容」。
> 此前两轮修补（长度阈值 + 词组抽取）治标未治本，本任务**推倒重来**，不再在现有启发式上打补丁。
> 授权：在 `.worktrees/W5`（task/W5 分支）实现；pytest 必须通过；GUI/真实数据实测交由用户；禁止在主树操作。

---

## 1. 为什么现在的实现无效（症状与根因）

### 1.1 症状

用户在真实游戏（HOROS，Ren'Py 8.3.7，数万行英文脚本）上点「从工程抽取候选」，
弹出的 50 个候选几乎全是**无法作为术语使用的碎片**，而真正需要统一翻译的
人名/地名没有浮出来，或被淹没。

### 1.2 根因（按权重排序）

1. **数据源错位**：抽取的对象是 `Segment.source`——这些是** Ren'Py 源码脚本里的
   自然语言句子**（对话/描述），不是结构化的标识符。在自然语言句子里数「大写开头的
   1~3 词组」，命中最多的是**普通句首单词**（I、The、When…已被句首剔除，但
   "Horos said that…" 这种真正人名和 "The Horos festival" 这种普通名词组
   无法靠大小写区分）。
2. **没有用最可靠的信号源**：Ren'Py 游戏里「谁是角色」有**权威定义**——
   `define xxx = Character("Horos")`。这个定义在 rpy 源码里是正则可扫的，
   比任何统计启发式都准。当前实现完全没碰它。
3. **strings 段 ≠ 术语**：strings 块（画廊标题、成就名、选项）是**待翻译文本**，
   不是术语；把整条计入候选，产出的就是"句子列表"。
4. **频次信号被稀释**：对话中人名出现次数远低于普通单词，1~3 词滑窗 + 计数的
   噪声淹没信号。

### 1.3 结论

**统计启发式在自然语言句子上没有出路。** 必须换成「权威定义优先 + 模型辅助」的策略。

---

## 2. 新算法（三信号源，按可靠性排序）

### 信号源 A：Ren'Py 角色定义（权威，必须实现）

正则扫描解包出的 `game/**/*.rpy`（注意：是**原始游戏脚本目录**，不是 tl 目录——
tl 里只有待翻译行）：

```python
# 匹配（覆盖常见形态，多行 define 用 try 逐行即可）：
# define hor = Character("Horos")            ← 最常见
# define hor = Character(_("Horos"))         ← 可翻译包装
# define h = DynamicCharacter('xxx')         ← 忽略（动态名）
CHAR_DEF_RE = re.compile(
    r'^\s*define\s+\w+\s*=\s*Character\(\s*_?\(\s*"(?P<name>[^"]+)"',
)
```

- 命中 → 直接作为**高置信候选**（count = 出现次数可标 999 或真实扫描 tl 前源文件的次数）；
- 同时抽取 `Color("#..")` 之前的 name 参数里带 `interact=False` 等复杂形态时
  **允许漏报**（宁缺勿滥），但**不允许误报**。

### 信号源 B：`old "..."` 字符串（strings 块的真正用途）

`translate chinese strings:` 块里的 `old "X"` 行是**游戏声明的可翻译字符串**，
其中 1~3 词的短条目（如 `old "Ian"`、`old "Stalkfap"`）是强信号——
游戏作者把它们单列成 strings，说明它们是**需要统一处理的独立单元**。

- 条件：`len(X) <= 24` 且 `X` 含英文字母（纯标点/纯数字跳过）；
- 这类候选与 A 信号合并去重。

### 信号源 C：AI 辅助识别（可选开关，config 加 `term_extract_use_ai: bool = True`）

- 用户点「从工程抽取候选」时，若 A+B 候选 < 20，追加一次 AI 调用：
  把前 N 段对话（或随机采样 200 行）发给模型，
  prompt：`列出这段英文游戏文本中的人名、地名、专有名词（游戏术语），每行一个，只输出名字本身`；
- 模型返回的词需在工程 source 中**实际出现 ≥2 次**才入选（防模型幻觉编造名字）；
- 失败/超时静默跳过（此信号是增益，不是依赖）。

### 明确不再做的

- ~~对自然语言句子做 1~3 词大写滑窗统计~~（根因 1，删除）；
- ~~strings 整条计入~~（根因 3，删除）。

---

## 3. 输出与交互

- 候选弹窗列：`候选词 | 来源(A=角色定义/B=old短串/C=AI) | 出现次数 | [译名输入] | [忽略]`
  —— 来源列让用户知道置信度；
- 排序：来源 A → B → C，同源按次数降序；
- 抽取基准目录：**`game_root/game/`（原始脚本），不是 tl/**——角色定义在源脚本里；
  若 tl 里已有 `old "X"` 也可扫（信号 B 兼容两处）。

---

## 4. 数据与代码落点

| 文件 | 改动 |
|---|---|
| `pytran/terms.py` | 重写 `extract_candidates`：签名改为 `extract_candidates(segments, profile, game_root=None, ai_helper=None, limit=50)`；实现信号 A/B/C；删除 `_dialogue_candidates` 滑窗统计与 `_strings_candidates` 整条计入 |
| `pytran/ui.py` | 抽取按钮调用时传入 `game_root`；弹窗加「来源」列 |
| `pytran/config.py` | `term_extract_use_ai: bool = True`（Field + ini + coerce） |
| `pytran/providers.py` | 暴露一个轻量 `complete_text(prompt) -> str`（不走 JSONL 协议，给信号 C 用；复用同一 client/超时/脱敏） |
| `tests/test_terms_extract.py` | 新文件：信号 A（define 正则各形态）、信号 B（old 短串阈值）、信号 C mock（返回幻觉词被出现次数过滤）、A+B+C 合并去重排序、空输入 |

---

## 5. 验收

### pytest（会话负责）

- 信号 A：三种 define 形态 → 全部命中；DynamicCharacter → 不命中；
- 信号 B：`old "Ian"` 入选、`old "Click to continue"`（>24 字符）不入选；
- 信号 C：mock 返回 `["Horos", "FakeName"]`，`FakeName` 在工程出现 0 次 → 被过滤；
- 合并：A 与 B 重复 → 去重且来源标 A；
- 空工程 / 无 game_root → 返回 `[]` 不抛错。

### 用户实测清单（交付说明必附，格式：操作→预期）

1. 选 HOROS 目录（已解包+反编译）→ 点「从工程抽取候选」→
   **预期**：候选含 `Horos`（来源 A）及 HOROS 实际角色名，不再出现普通句首词或整句；
2. 来源列显示 A/B/C；
3. 若配置了 API Key，来源 C 的 AI 候选出现（无 Key 时 A/B 仍工作）；
4. 全部忽略后重新抽取 → 不再重复弹已忽略项。

---

## 6. 边界与红线

- 不改冻结契约（models/config/interfaces——`config.py` 加字段属于既有惯例，
  按惯例新增字段需总控确认，本任务书即为确认）；
- 信号 C 的 AI 调用：最多 1 次/次抽取、timeout 60s、失败静默——**不得**让它成为抽取失败的原因；
- 不删 `pytran/terms.py` 里其它在用函数（`profile_to_rows`/`rows_to_terms`/
  `detect_duplicate_originals`/`apply_terms`）；
- 老启发式（`_dialogue_candidates` 等）删除前确认测试无引用。

---

## 7. 补充分析：如何更好提取「角色姓名等常用专有名词」

> 本章是对 §1–§2 的深化：任务书已给出三信号源框架，以下补充**任务书未覆盖的
> 关键洞察与落地细节**，均已在代码（`pytran/terms.py`、`parser.py`、`ui.py`）
> 与真实工程数据（`.w4b_real_game`、`logs/.horos_*`）上核实过。

### 7.1 被任务书忽略的最强信号：解析器已经在产出「角色姓名」

`pytran/parser.py` 的 `_extract_dialogue`（L94）把对话行拆成
`(role, quote, text, suffix)`，其中 **`role` 就是说话人名**：
Ren'Py 源码里 `define hor = Character("Horos")` 定义好后，所有台词写成
`hor "How are you"`，而 **tl 目录下的 `translate` 块同样保留 role 前缀**——
`Segment.role` 已经把这个结构信号咽进去了，但当前 `extract_candidates`
（`terms.py` L132–166）**只读 `s.source`，完全没看 `s.role`**。

这是零成本的第四信号源，且可靠性仅次于信号 A：

| 信号 | 可靠性 | 成本 | 现状 |
|---|---|---|---|
| A：`define x = Character(...)` 正则 | 极高（权威定义） | 需 `game_root` + 已解包 | 任务书已列 |
| **D：`Segment.role`（对话行前缀）** | **高（结构信号，非统计）** | **零（已解析好）** | **完全未用** |
| B：`old "..."` 1–3 词短串 | 中高 | 需扫 tl | 任务书已列 |
| C：AI 识别 | 中（需 ≥2 次出现过滤） | 需 API Key | 任务书已列 |

实现建议：`role` 值按 `Counter` 计数后，凡出现 ≥2 次（或 ≥1 次但长度
2~24 字符且不是 `narrator`/`centered` 等引擎内置名）即入选，来源标 `D`。
它有一个 A 给不了的好处：**A 只在 `game/` 原始脚本里，D 直接在用户
导入的 tl segments 里**——即使没有 `game_root`（纯文件模式，`ui.py` L796
按钮在 `segments` 非空时就可点），信号 D 依然工作。

### 7.2 `game_root` 拿不到时的降级链（任务书 §5 要求「无 game_root → `[]` 不抛错」，但 `[]` 是劣化体验）

`ui.py` L757 的置灰条件是 `bool(self.project.segments)`，而 `Project.game_root`
默认为 `""`（`models.py` L119，纯文件模式合法）。所以「有 segments 但无
game_root」是**常态而非异常**。建议抽取内部按以下顺序尝试定位 rpy 源码，
任一命中即可解锁 A/B：

1. `project.game_root / "game"` —— 标准 Ren'Py 布局；
2. `project.game_root / "game" / "tl" / <language>` —— tl 里的 `old "..."`；
3. `work_dir / "bak"` / `work_dir / "output"` —— 写回前备份与导出目录；
4. segments 自身的 `file` 字段反推：`game_root / game / seg.file`。

注意 §2 说「扫描 `game/**/*.rpy`」，而 `unren.decompile`（`unren.py` L349）
默认 `out_dir = rpyc.parent`，即**反编译产物就落在 `game/` 里、与 rpyc 同目录**。
所以「解包+反编译后点抽取」这个验收场景（§5 用户实测清单第 1 条）是自洽的：
`game/` 下已有解出的 `.rpy`。但**解包前**（只有 rpyc）信号 A 必然为空——
这一点必须在 §5 用户实测清单里写明，否则用户会误判为 bug。

### 7.3 信号 B 的真实形态与阈值（任务书 §2 的 `old "..."` 假设需修正）

任务书假设 `translate chinese strings:` 块里有 `old "X"` 行。实际上
`parser.py` 对 `old`/`link` 的处理分两处：

- L251–259：**`translate` 块内**的 `old "X"` 会被 `_LINK_RE` 命中并作为
  `kind="dialogue"` 段发出——这里的 `old "X"` 是**上一轮翻译的残留/引用**，
  内容多为整句，不是术语；
- L298–306：**`strings` 块内**的 `old "X"`/`new "X"` 才是 `_OLD_NEW_PAIR_RE`
  命中的 `kind="strings"` 段——这才是游戏作者显式声明的可翻译短串。

因此信号 B 的正确实现是：**直接读 `segments` 里 `kind == "strings"` 的段**
（源码里就是那些 `old "X"` 短串），而不是自己去扫 rpy 文本找 `old`。
这也让信号 B **不需要 `game_root`**，与信号 D 一样在任何模式下可用。

阈值上，`len(X) <= 24` 的任务书条件会放进一些非术语短串（如
`"Click to continue"` 恰好 18 字符）。建议叠加两个紧缩条件：

- `X` 词数 ≤ 3（1~3 词，对齐任务书 §1.3 对「词组」的原始口径）；
- 至少一个词**首字母大写**（真专名几乎必满足；`old "click to continue"`
  这类全小写 UI 文案被排除）。

### 7.4 反编译产物不一定是文本：`.rpyc` 里没有可扫的字符串

已在真实数据上验证：`logs/.horos_terms/script.rpyc`（578KB）用 ASCII 与
latin1 解码后，`Character`/`Horos`/`Stalkfap`/`old `/`translate` 全部为
0 次命中，长度 ≥10 的可打印串只有 33 条且全是乱码——**RPC2 是 pickle +
压缩的 AST，正则扫不出任何东西**。这从反面证明任务书 §1.2 根因 1 的判断
是对的：对未解包的工程做任何文本启发式都无效，**必须先解包/反编译**。

推论：信号 C（AI）如果发给模型的是**原始 rpyc 的内容**，同样无效；它必须
采样 **segments 的 `source`**（即 tl 里已解析出的英文原文），这也是任务书
§2 信号 C 说「前 N 段对话（或随机采样 200 行）」的本意。

### 7.5 频率统计的新位置：从「启发式主信号」降级为「去噪过滤器」

根因 4「频次信号被稀释」的解法不是提高阈值，而是**换分母**。建议把
`count` 的计算口径从「在全部 source 里数大写词组」改为：

- 信号 A/D 的 count = 该名在 **segments 的 `role` 或 `source`** 中实际
  出现的次数（真实、可解释，用户看到「出现 37 次」能直接判断重要性）；
- 信号 B 的 count = 该串作为 `strings` 段出现的次数；
- 信号 C 的 count = 模型返回词在 source 中的实际出现次数（任务书 §2
  的「≥2 次才入选」即此，保留）。

这样「出现次数」列（§3 输出）才真正有信息量，而不是像现在这样数出
`The 120`、`Horos 8` 这种不可比的数字混在一起。

### 7.6 排序与去重的具体规则（补 §3 的模糊处）

任务书 §3 说「来源 A → B → C，同源按次数降序」，补上落地细节：

1. **去重键**：`term.strip()` 后**大小写不敏感**比较，但**保留首次出现的
   原始大小写**作展示。`Horos` 与 `horos` 是同一个术语，不该占两个候选位。
2. **来源优先级**：改为 `A > D > B > C`（D 比 B 可靠，见 §7.1 表格）；
   A 与 D 撞名时来源标 A（任务书 §5「A 与 B 重复 → 来源标 A」的推广）。
3. **`limit` 的口径**：任务书 §4 写 `limit=50`。建议 A/D 候选**不占 limit**
   （角色名通常 < 30 个，全部展示），B/C 候选用剩余名额填充——
   否则 50 个名额可能被 B 的短串吃光，挤掉真正的人名。
4. **反向过滤**：除 §5 已有的「已在 terms / exempt_english」外，还应排除
   Ren'Py 引擎内置 role 名：`narrator`、`centered`、`vcentered`、
   `thought`。这些在 `.w4b_real_game/renpy/common/00definitions.rpy` L436–441
   有明确定义，会被解析器原样收进 `role`，是纯噪声。

### 7.7 对 §4 代码落点表的修订

| 文件 | 改动 |
|---|---|
| `pytran/terms.py` | 重写 `extract_candidates`：签名改为 `extract_candidates(segments, profile, game_root=None, ai_helper=None, limit=50)`；实现信号 A/B/C/**D**；删除 `_dialogue_candidates` 滑窗统计与 `_strings_candidates` 整条计入 |
| `pytran/ui.py` | 抽取按钮调用时传入 `game_root`；弹窗加「来源」列；弹窗行改为「候选词\|来源\|次数\|译名\|忽略」五列 |
| `pytran/config.py` | `term_extract_use_ai: bool = True`（Field + ini + coerce） |
| `pytran/providers.py` | 暴露一个轻量 `complete_text(prompt) -> str`（不走 JSONL 协议，给信号 C 用；复用同一 client/超时/脱敏） |
| `tests/test_terms_extract.py` | 新文件：信号 A（define 正则各形态）、**信号 D（role 计数、引擎内置名过滤、无 game_root 仍工作）**、信号 B（old 短串阈值）、信号 C mock（返回幻觉词被出现次数过滤）、A+B+C(+D) 合并去重排序、空输入 |

### 7.8 需要任务书作者确认的两个口径变更

1. **信号 B 的实现来源**：从「自己扫 rpy 找 `old`」改为「读
   `kind == "strings"` 的 segments」。优点是不依赖磁盘、不重复解析逻辑；
   代价是拿不到 tl 里**未导入**的 `old "X"`。若需要覆盖未导入文件，
   仍须保留 `game_root` 扫描分支作为补充。
2. **A/D 不占 `limit`**：这会改变 `limit=50` 的既有契约语义（现在是
   「最多返回 50 个」）。建议改为「A/D 全量 + B/C 补足到 50」，
   并让 `extract_candidates` 在 docstring 里写明。
