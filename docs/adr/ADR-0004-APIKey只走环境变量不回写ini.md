# ADR-0004：API Key 只走环境变量，不回写 ini

- 日期：2026-10-09
- 状态：已接受（2026-10-13 修订：api_key 恢复写入 config.ini——文件在 .gitignore，红线是「不入 git」而非「不入本机配置」；环境变量优先级不变；决策第 2 条作废，决策第 1/3 条与日志脱敏保留）
- 影响范围：T0 config.py、W5 设置界面、W6 打包（§12）

## 背景

2.0 的 `config.ini` 把真实 API Key 提交进了 git 历史（审查结论：需吊销 + filter-repo 清洗）。
3.0 必须从机制上杜绝复发。

## 决策

Key 读取顺序：环境变量 `TL_API_KEY` > config.ini 既有值。
`Config.save()` **不把 api_key 写回 ini**（空值跳过，避免覆盖；UI 修改 Key 时提示走环境变量或用户自管 ini）。
仓库提供 `config.example.ini`（无 key），真实 `config.ini` 进 `.gitignore`。

## 理由

- 明文 key 一旦入库就只能靠历史清洗补救，成本远高于入口约束；
- GUI 用户可接受首次运行向导引导，CLI 用户习惯环境变量。

## 后果

- W5 设置页保存 Key 时需要明确提示（或提供「写入 ini 但自行承担」的显式选项）；
- W6 的打包脚本必须断言产物中无 `config.ini`。
