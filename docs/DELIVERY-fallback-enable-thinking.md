# 交付说明：回退模型专用思维链开关

- **分支**：`feature/enable-thinking-toggle`（基于 `master` @ `0ae4ead`）
- **状态**：已实现、已自测、**未提交**，待审核人员审核
- **回归**：`609 passed`（上一版 588 + 新增 21）
- **测试环境**：Windows / Python 3.13.3 / `.venv` / PyQt6 offscreen
- **前置**：本改动叠加在 `docs/DELIVERY-fallback-after-failures.md` 之上

---

## 一、需求

> 给回退模型设置思维链开关

改造前的状态：`enable_thinking` 是**全局单一开关**，主模型和回退模型共用。
用户无法做到"主模型保留思维链、回退模型关掉"（或反之）。

### 已确认的默认值口径

经询问确认：**回退模型用独立开关，默认开启**（`fallback_enable_thinking = true`）。
即默认行为与改造前完全一致——升级后回退请求逐字节不变，不会出现"一升级
回退模型就变慢"的意外。

---

## 二、界面位置

设置对话框「常规」页，回退相关三项聚在一起：

```
row 2: 模型名称                                    QLineEdit
row 3: ☑ 允许思维链（关闭可显著提速、省 token）        QCheckBox   ← 主模型
row 4: 回退模型 (可选，漏翻重试时切换)                QLineEdit
row 5: 失败几次后切回退模型 (0=不回退)                QLineEdit
row 6: ☑ 回退模型允许思维链（独立于主模型）            QCheckBox   ← 新增
row 7: max_tokens（单次请求上限）                    QLineEdit
```

已 offscreen 实测：`失败几次后切回退模型 row=5`、`新勾选框 row=6`，相邻且在其后。
默认勾选，带 241 字 tooltip 说明「什么时候该关」及「只在真的切到回退模型时生效」。

---

## 三、行为

### 3.1 取值语义

| 主模型 `enable_thinking` | 回退 `fallback_enable_thinking` | 走主模型时 | 走回退模型时 |
|---|---|---|---|
| 勾 | 勾（默认） | 不发参数（允许推理） | 不发参数（允许推理） |
| 勾 | 取消 | 不发参数 | **发 `enable_thinking=false`** |
| 取消 | 勾 | **发 `enable_thinking=false`** | 不发参数 |
| 取消 | 取消 | 发 `enable_thinking=false` | 发 `enable_thinking=false` |

**关键**：按**本次实际使用的模型**取开关。第 2、3 行是本次新增的能力，
也是两个字段必须独立的理由。

### 3.2 一个容易写错的边界

`use_fallback_model=True` **不等于**真的走了回退模型。看
`protocol.build_request_params`：

```python
model = config.fallback_model if use_fallback_model and config.fallback_model else config.model
```

若用户没配 `fallback_model`，即使引擎传了 `use_fallback_model=True`，
实际请求的仍是主模型。因此开关选择必须带上同样的条件：

```python
if use_fallback_model and getattr(self._config, "fallback_model", ""):
    thinking_on = getattr(self._config, "fallback_enable_thinking", True)
else:
    thinking_on = getattr(self._config, "enable_thinking", True)
```

只写 `if use_fallback_model:` 会导致「未配回退模型时用错开关」，
已用 `test_fallback_switch_ignored_when_no_fallback_model_configured` 锁住。

---

## 四、改动文件

| 文件 | 改动 |
|---|---|
| `pytran/config.py` | +1 字段 `fallback_enable_thinking`（changelog / `_DEFAULT_INI` / `_INI_SECTION_MAP` / dataclass / `_coerce_bool` 归一化） |
| `pytran/providers.py` | `translate_batch` 按实际使用的模型选开关（原先只读全局 `enable_thinking`） |
| `pytran/ui.py` | 新增勾选框 `cb_fallback_enable_thinking`，紧跟「失败几次后切回退模型」；`_coerce_setting` 布尔清单增该字段；`save()` 收集勾选状态 |
| `tests/test_fallback_enable_thinking.py` | **新增** 21 项 |

---

## 五、`config.py` 契约变更（第四次追加字段）

本次仍是**纯追加**，未改动任何既有字段语义：

- `_DEFAULT_INI` 增 `[Model] fallback_enable_thinking = true`
- `_INI_SECTION_MAP` 增 `("Model", "fallback_enable_thinking")`
- dataclass 增 `fallback_enable_thinking: bool = True`
- `__post_init__` 增 `_coerce_bool(..., default=True)`
- 文件头 changelog 增 2026-10-25 条目

**审核关注点**：`config.py` 头部声明 T0 冻结。累计已第四次追加
（`[Review]` 4 字段 → `enable_thinking` → `fallback_after_failures` → 本次）。
若审核认为应改为"同一主题合并为一个复合字段"（例如 `thinking = {main, fallback}`），
本次改动量很小，可随时调整；但扁平字段与现有 `_INI_SECTION_MAP` 机制最贴合，
改成复合结构需要动 `Config.save/load` 的读写逻辑，风险更大。

---

## 六、测试

```
609 passed in 78.97s
```

新增 21 项覆盖：

- **配置层**：默认 True、模板含键、ini 往返（存 false → 读回 False 且非字符串）、
  旧 ini 缺键 → True、8 种 configparser 布尔写法、两字段可各自独立取值。
- **provider 层**：主模型用主开关、回退模型用回退开关、回退开关开时即使主开关关
  也不发参数、两开关都关时两个模型都发、未配回退模型时不误用回退开关、
  属性缺失时按 True 不抛异常。
- **UI 层**：勾选框存在且默认勾选、文案含「回退模型」、有 tooltip、
  取消勾选后落盘为 bool `False`（不是字符串）。

---

## 七、用户验证步骤

1. 设置 → 常规 → 在「失败几次后切回退模型」下方找到
   **「回退模型允许思维链（独立于主模型）」**，默认已勾选。
2. 取消勾选 → 保存 → 确认 `config.ini` 的 `[Model]` 段出现
   `fallback_enable_thinking = False`，且 `enable_thinking` 未被牵连。
3. 想只让回退模型关思维链、主模型保持开启：主开关保持勾选，
   只取消这一个勾选框即可。
