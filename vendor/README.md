# vendor/ — 内置第三方工具源码

根据 ADR-0005，将上游 `unrpyc` 与 `rpatool` 以**明文源码**内置，替代原
`UnRen-forall.bat` 的 base64+cab+PowerShell 运行时释放方式，便于审计、许可合规
与单元测试，并去掉对系统组件 `expand.exe` 的依赖。

## 内容

| 文件 | 来源 | 许可 | 说明 |
| --- | --- | --- | --- |
| `rpatool.py` | Shizmob/rpatool（RPA-3.0 格式公共域参考实现） | 公共域 | 本地化、stdlib-only 重实现的 RPA-3.0 解包/打包，含 `extract`/`create` 两个入口 |
| `unrpyc.py` | 薄封装入口（本项目） | MIT | 处理 `tests/fixtures` 合成 `fake_unrpyc` 的 `.rpyc`（内嵌原始 `.rpy`），并可在 `vendor/unrpyc_real/` 存在真实上游 `unrpyc` 时自动委派（目录检测 + 子进程桥，见 ADR-0008 §8.5） |
| `unrpyc_real/` | CensoredUsername 原版 + F95Sam 编辑版 `unrpyc` | MIT（Yuri K. Schlesner） | **真实上游 drop-in**（W4b 落地）：commit `44febb0`（2019-10-07），含 `unrpyc.py`、`deobfuscate.py`、`decompiler/*` 与 `LICENSE`。模块加载时 `import renpy`，故由 `runner.py` 经子进程桥在持有真实 Ren'Py 引擎的解释器里运行 |

上游真实 unrpyc（CensoredUsername 原版 + F95Sam 编辑版）已于 **W4b** 通过
`UnRen-forall.bat` 自解包批处理还原并 vendored 到 `vendor/unrpyc_real/`，其接入
方式见 ADR-0008 的 drop-in 升级路径：封装检测到 `vendor/unrpyc_real/` 目录即自动
委派，经一次性子进程到 `vendor.unrpyc_real.runner` 完成真实解码（每进程一次
`import renpy`，可批量）。

## unrpyc_real 上游兼容性补丁（W4b，记入 §0.5）

上游 commit `44febb0`（2019）针对 Ren'Py 7.x；在 Ren'Py 8.3.7（python 3.12）上
做了三处最小适配，保留上游功能性：

1. `unrpyc.py` `PyCode.__setstate__`：兼容 4-tuple / 5-tuple 两种 co 状态。
2. `decompiler/__init__.py`：对缺失 `hide` 属性的 AST 节点做 `getattr` 守卫。
3. `decompiler/magic.py` `FakePackageLoader`：PEP 302 `find_module`/`load_module`
   在 3.12 已移除，改为 `find_spec`/`create_module`/`exec_module`。

> 偏差（§0.5）：该 2019 版本对**部分** Ren'Py 8.3.7 的 `.rpyc` 节点
> （`PyExpr`/`Label`/`Signature`）会抛 `AttributeError`，属上游与 8.x AST 演进
> 不同步，非封装缺陷；不可解样例由封装抛明确 `UnrpycError`。

## 许可与署名保留

- `rpatool.py` 头注释标注其源自 Shizmob 的公共域 RPA-3.0 参考实现，并保留原格式
  说明。
- `unrpyc_real/` 连同其 `LICENSE`（MIT, Yuri K. Schlesner）与作者署名一并提交，
  已在各上游文件头追加归属注释，并记录上游 commit `44febb0`（ADR-0005 后果条款）。

## 升级

上游更新时手动同步，并在本文件记录上游 commit hash。
