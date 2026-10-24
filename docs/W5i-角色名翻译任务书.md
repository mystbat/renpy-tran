# W5i 任务书：翻译请求携带角色名——让 AI 知道谁在说话

> 任务来源：用户实测问询（2026-10-21）确认的协议缺陷。
> 现状：`chunk.lines()` 只传 `(uid, source)`，`role` 字段在整条链路中被丢弃。
> 模型收到的每行只有 `编号|原文`，完全不知道说话者是谁——
> 「每个角色的说话风格保持前后一致」在当前实现下没有足够信号支撑。
> 授权：`.worktrees/W5` + `task/W5`；pytest 必须；GUI 实测归用户；禁主树。

---

## 1. 改动范围（三处，全部非冻结文件）

### 1.1 `segmenter.py` — `Chunk.lines()` 携带 role

```python
# 现在
def lines(self) -> list[tuple[int, str]]:
    return [(s.uid, s.source) for s in self.segments]

# 改为
def lines(self) -> list[tuple[int, str, str]]:
    """返回 (uid, role, source) 列表。role 可为空字符串（旁白/系统）。"""
    return [(s.uid, s.role, s.source) for s in self.segments]
```

### 1.2 `protocol.py` — `build_user_prompt` 行格式

```python
# 现在
body = "\n".join(f"{uid}|{text}" for uid, text in numbered_lines)

# 改为（role 非空时插入，空时留空位）
body = "\n".join(
    f"{uid}|{role}|{text}" if role else f"{uid}||{text}"
    for uid, role, text in numbered_lines
)
```

系统提示词 `DEFAULT_SYSTEM_PROMPT` 的第 1 条同步修改：

```
1. 只输出 JSON 行，每行一个：{"i": <编号>, "t": "<译文>"}；不要输出任何解释、空行或代码块。
```
→
```
1. 用户文本格式为「编号|角色名|文本」。角色名表示该行的说话者。
   只输出 JSON 行，每行一个：{"i": <编号>, "t": "<译文>"}；不要输出任何解释、空行或代码块。
   角色名仅用于判断说话者身份和风格，不要把它写进译文。
```

### 1.3 `providers.py` — `translate_batch` 签名

```python
# 现在
def translate_batch(self, lines: list[tuple[int, str]], ...) -> ...:

# 改为
def translate_batch(self, lines: list[tuple[int, str, str]], ...) -> ...:
```

`_normalize_context` 同步（前文参考也带 role 时格式一致，但 context 不需要 role——
前文参考的目的是语气衔接，显示格式用引号包裹即可）。

### 1.4 `engine.py` — `_translate_round` 适配

`chunk.lines()` 返回三元组后，`tasks` 构建和 `_apply_result` 调用不需要改逻辑，
只需确保传给 `translate_batch` 的 `lines` 是三元组。

`_apply_result` 的 `requested` 集合从 `{uid for uid, _src in lines}` 改为
`{uid for uid, _role, _src in lines}`。

---

## 2. 不改的东西（明确排除）

- **不把 role 写进译文**：模型收到角色名用于判断风格，但输出 `t` 字段只含翻译后的台词文本。系统提示词第 1 条已明确「角色名仅用于判断说话者身份和风格，不要把它写进译文」。
- **不改冻结契约**：`interfaces.py` 的 `Provider.translate_batch` 签名用了 `list[tuple[int, str]]`——这是类型注解层面的变化。按 ADR-0006 前例（任务书批准即契约变更授权），本任务书批准将签名改为 `list[tuple[int, str, str]]`。实现者在交付说明中记录。
- **不改前文参考的格式**：context 保持 `(src, dst)` 二元组（不加 role），原因：前文参考的目的是语气衔接，显示的是已翻译的对照，加角色名会占 token 且模型从上下文能推断。

---

## 3. 测试（pytest，≥6 用例）

| # | 用例 | 断言 |
|---|---|---|
| 1 | `chunk.lines()` 返回三元组 | `(uid, role, source)` |
| 2 | `build_user_prompt`：role 非空 → 行含 `角色名|` | 格式正确 |
| 3 | `build_user_prompt`：role 为空 → 行含 `||`（空位） | 格式正确 |
| 4 | `build_system_prompt`：含「角色名表示该行的说话者」 | 新提示词 |
| 5 | 端到端：mock provider 收到的 lines 是三元组 | 引擎→provider 传递完整 |
| 6 | 回归：JSONL 主路径解析不受影响（模型输出仍为 `{"i":n,"t":"..."}`） | 不因输入格式变化而破坏解析 |

## 4. 用户实测清单（交付说明必附）

1. 翻译一段两个角色交替说话的文本 → **预期**：两个角色的语气/用词风格有明显区分；
2. 翻译规则加「Horos 说话粗鲁直白，Lena 说话温柔含蓄」→ 翻译后对比两人台词风格；
3. 空角色行（旁白）正常翻译，不出现角色名混入。

## 5. 交付

- 分支 `task/W5` 继续；red→green；交付说明含 pytest 输出 + git log + 实测清单；
- 契约变更（Provider 签名）在交付说明记录；
- 完成后交总控审核（重点：`build_user_prompt` 行格式与系统提示词的一致性、`parse_response` 不受影响）。
