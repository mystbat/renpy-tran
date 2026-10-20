# BUGFIX(layer0-fence)：第 0 层解析先剥代码栅栏——围栏内对象流不再整批失败

- 日期：2026-10-20
- 触发：用户实测反馈（翻译整批失败：`响应解析失败 uid=1..20`，
  `ProtocolError: 响应中没有任何可解析的编号行`）
- 状态：✅ 已修复（365 全量用例通过，新增 3 用例）
- 关联：本文件与 `docs/BUGFIX-old-new-pair.md` 同日两例；layer0 首次引入见
  commit `1492040`（同日第一例：无括号多行数组）。

---

## 1. 现象

翻译一批 20 行时整批失败：

```
响应解析失败（不重试，交引擎改策略）：uid=1..20
pytran.interfaces.ProtocolError: 响应中没有任何可解析的编号行
（原始响应前 200 字符：'{\n  "i": 1,\n  "t": "我们径直走到那里…"\n},\n{…'）
```

原始响应内容其实完全正确（对象形态与首次修复的「无括号多行数组」一致），
却三层解析全灭。

## 2. 定位

对比首例修复（layer0）的本地复现：**同样的对象流裸文本能解析**。
差异在报错原文的两点：

1. 响应被模型包在 **```json 代码栅栏**里。第 0 层 `_parse_json_array`
   对原始 `raw` 判 `startswith("[")` / `startswith("{")`——看到的是
   ` ```json ` 字符，直接返回 None；栅栏剥离（`_strip_fence_lines`）
   只服务于逐行层。
2. 逐行层拿到的是对象内部**碎行**：`{`、`"i": 1,`、`"t": "…"`、`},`——
   每行单独都不含完整的 `{...}` 结构，`_parse_one_line` 三层全败。

结果：整批 20 行全部进 `missing` → FAILED，且按 §4.3 协议错误不重试。

## 3. 修复方案

`_parse_json_array`（pytran/protocol.py）入口先对文本做栅栏剥离，再走
原有判定/解析：

- 顶部/底部**独立围栏行**（` ```json ` / ` ``` ` 单独成行）→ 剥掉；
- **单行围栏包裹**（` ```json {...} ``` ` 一行，DOTALL）→ 取内部文本；
- 之后逻辑不变：`[...]` 直接解析；无括号对象流包 `[ ]` 再试；
  末尾多余逗号容忍；非法转义仍交逐行层兜底（回归路径不破坏）。

## 4. 测试

新增（tests/test_protocol_layer0.py）：

- `test_fenced_no_bracket_multiline_objects`：用户实测形态
  （围栏 + 无括号多行对象流）→ 整批回收，无 missing；
- `test_fenced_json_array`：标准围栏数组 → 整批回收；
- `test_bad_escape_falls_through_to_line_layer`：非法转义仍走逐行层。

回归：`python -m pytest tests -p no:cacheprovider` → **365 passed**。

## 5. 影响面

- 仅第 0 层入口的预处理，三层解析优先级与护栏逻辑不变。
- 此前因该形态整批失败的批次，重新翻译即可恢复；引擎的逐行重发
  （§5.3）与预算控制不受影响。
