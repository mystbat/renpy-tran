# ADR-0010：Exporter 以可选关键字参数扩展（不修改冻结契约）

- 日期：2026-10-13
- 状态：已接受
- 影响范围：`interfaces.Exporter` 契约（§6.3/§6.4/§6.5/§15）、`pytran/exporter.py`

## 背景

W5 的 `ExporterImpl` 在冻结契约 `interfaces.Exporter` 的四个方法签名之外，
额外实现了三项能力：

1. **强制绕过导出熔断**：§6.3 允许用户在「匹配率不足」等情况下仍导出（保留未译原文）；
2. **事务化写回**：`write_back` 把译文写回游戏目录，并接入 journal 审计事务（§15.2）；
3. **批量接受附带 exempt 收集**：`bulk_accept` 在通过一批行的同时，可把保留原文残片
   并入 `exempt_english`（§6.5）。

这三项能力落地的形态是**临时扩展**（exporter.py 文件头与 W5 交付说明均已标注），
均未改动 `interfaces.py`：在既有的 `export_rpy` / `import_csv` / `bulk_accept` 上
**追加可选关键字参数**（`export_rpy_forced`、`write_back_project`、`bulk_accept(add_exempt=)`），
保持向后兼容（新参数均有默认值）。

W6 在 `cli.py` 正式消费了这些扩展（`cmd_game` 的 L2 写回、`export-csv`/`import-csv`），
因此需要一个 ADR 把「这种扩展方式」正式确立为工程约定。

## 决策

1. **冻结契约（`interfaces.py`）维持只读**：`Exporter` 四方法签名不变（T0 冻结）。
   任何超出原签名的语义一律以「实现侧可选关键字参数 / 额外方法」承载，**不**回填进 Protocol。
2. **扩展以「可选 kwargs + 附加方法」双轨落地**：
   - 与既有方法语义同根、仅多一个开关的 → 用**可选关键字参数**
     （如 `export_rpy(..., write_back=False, journal=None)` 的强制/写回变体、
     `bulk_accept(..., add_exempt=False)`）；
   - 与既有方法职责正交、不便塞参数的 → 用**附加方法**
     （如 `write_back_project(project, work_dir, journal)`）。
3. **向后兼容硬约束**：所有扩展参数必须有默认值，使「只认冻结契约」的调用方
   （如历史测试、未来替身）无需改动即可运行。
4. **契约变更建议**：若未来确有普遍需求，再走「总控裁决 → 正式扩充 `interfaces.Exporter`
   + 分配新 ADR」流程；在此之前，本 ADR 是这些临时扩展的**官方解释**，不构成契约修改。

## 理由

- 契约稳定性优先：`interfaces.py` 是 T0 冻结文件，W5/W6 不得修改；以可选参数扩展
  既拿到了能力，又不破坏契约，符合「契约变更建议」走交付说明的纪律；
- 向后兼容：可选 kwargs 让既有调用方零改动，也避免测试大规模重写；
- 单一职责：把「强制/写回/豁免收集」这些 §6.3/§6.5/§15 的跨职责能力放在实现层，
  契约层保持「四个原子动作」的精简心智模型；
- W6 已消费并验证了这套扩展（`cli.game` L2 写回、`export-csv`/`import-csv`、
  `migrate` 走 `export_rpy_forced` 的熔断后路径），落地经实战检验。

## 后果

- `interfaces.Exporter` 不被修改（W5/W6 均遵守）；
- `exporter.ExporterImpl` 对外暴露：`export_rpy`、`export_rpy_forced`、
  `export_csv`、`import_csv`、`write_back_project`、`bulk_accept`（含 `add_exempt`）、
  `bulk_undo`；
- 调用方（`cli.py`）必须显式传 `journal` / `write_back=True` 才触发事务化写回，
  默认行为仍是「仅导出 output/，不碰游戏目录」（§14/§15 防守）；
- 后续若冻结契约正式扩充，需同步更新本 ADR 并在交付说明登记「契约变更建议已落地」；
- 测试（test_exporter）已覆盖：熔断触发、`export_rpy_forced` 绕过、uid 主键回灌、
  不静默覆盖、快照+撤销、add_exempt 收集。
