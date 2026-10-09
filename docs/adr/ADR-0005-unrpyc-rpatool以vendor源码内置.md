# ADR-0005：unrpyc/rpatool 以 vendor 源码内置，替代 bat 解码链

- 日期：2026-10-09
- 状态：已接受
- 影响范围：W4 unren.py（§8）、W6 打包

## 背景

UnRen-forall.bat 以 base64+cab+PowerShell 在运行时释放 unrpyc/rpatool，依赖 expand.exe，
且补丁文件用 base64 隐藏内容。3.0 需要可审计、可维护、可测试的内置方式。

## 决策

unrpyc（CensoredUsername 原版 + F95Sam 编辑版）与 rpatool（Shizmob）以**明文源码**进 `vendor/`，
保留原许可文件与作者署名；四个游戏补丁（console/quick/skip/rollback）以明文模板常量保存在
`unren.py`，支持热键替换；移除 base64/cab/PowerShell 解码链与 expand.exe 依赖。

## 理由

- 源码可审计（base64 隐藏无安全收益，只妨碍审查）；
- 可直接做单元测试与许可合规；
- 去掉对系统组件（expand.exe）的依赖。

## 后果

- W4 合并时需核对 `vendor/` 含 LICENSE 与上游 commit 来源说明；
- 上游更新时手动同步（记录上游 commit hash 于 vendor/README）。
