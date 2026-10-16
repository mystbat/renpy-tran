# vendor/ — 内置第三方工具源码

根据 ADR-0005，将上游 `unrpyc` 与 `rpatool` 以**明文源码**内置，替代原
`UnRen-forall.bat` 的 base64+cab+PowerShell 运行时释放方式，便于审计、许可合规
与单元测试，并去掉对系统组件 `expand.exe` 的依赖。

## 内容

| 文件 | 来源 | 许可 | 说明 |
| --- | --- | --- | --- |
| `rpatool.py` | Shizmob/rpatool（RPA-3.0 格式公共域参考实现） | 公共域 | 本地化、stdlib-only 重实现的 RPA-3.0 解包/打包，含 `extract`/`create` 两个入口 |
| `unrpyc.py` | 薄封装入口（本项目） | MIT | 处理 `tests/fixtures` 合成 `fake_unrpyc` 的 `.rpyc`（内嵌原始 `.rpy`），并可在 `vendor/unrpyc_real/` 存在真实上游 `unrpyc` 时自动委派（目录检测 + 子进程桥，见 ADR-0008 §8.5） |
| `unrpyc_real/` | CensoredUsername/unrpyc `master` 分支 | MIT（Yuri K. Schlesner、CensoredUsername、Jackmcbarn） | **真实上游**（v2.0.4，2026-02 升级）：含 `unrpyc.py`、`deobfuscate.py`、`decompiler/*` 与 `LICENSE`。**不再依赖真实 `renpy` 引擎**（自行构造伪 `renpy` 包），故可直接在项目解释器内运行（Python ≥ 3.9） |

上游真实 unrpyc 已 vendored 到 `vendor/unrpyc_real/`。其接入方式见 ADR-0008 的
drop-in 升级路径：封装检测到 `vendor/unrpyc_real/` 目录即自动委派。

## unrpyc_real 上游版本：v2.0.4（2026-02 升级）

- 上游仓库：CensoredUsername/unrpyc（`master` 分支，即 Unrpyc v2.x 线）
- tag / commit：`v2.0.4` / `3ae8334ed71a05535927dcc559663d3aca51215b`（2026-02-23）
- 取代原先 vendored 的 **2019-10-07 `44febb0`**（Ren'Py 7.x 时代版本）
- 与旧版不同：**上游源码一字未改地引入**，不再需要任何本地兼容补丁
  （旧版的「W4b compatibility」三处 shim 已随旧源码移除）

升级动因与逐条修正（详见 `docs/adr/ADR-0008-*.md` 的「2026-02 修订」）：

| # | 旧 `44febb0` 的缺陷 | v2.0.4 |
| --- | --- | --- |
| 1 | `read_ast_from_file()` 把 slot **字典**喂给 `zlib`，导致 `TypeError`，或用户实际遇到的 `Error -5 ... incomplete or truncated stream` | 显式取 `chunks[1]`，异常改为有类型的 `BadRpycException` |
| 2 | `--try-harder` 全崩：`deobfuscate.py` 用 Python 2 的 `e.message` / `ord(int)` / int-vs-str | 已是 Python 3 实现，`--try-harder` 可用 |
| 3 | 不认识 Ren'Py 8.x AST（`Label._name`、`Signature.extrapos`、`Style.take`、`PyExpr.linenumber`…），SDK launcher 样本 22/37 失败 | SDK launcher 样本 36/37 |

实测：用户真实游戏 20 个 `.rpyc` **20/20** 解码成功（旧版走错分支时 0/20）。

### 旧版「必须子进程 + 真实 Ren'Py 引擎」的约束已解除

旧版 import 期即 `import renpy.object`，只能在持有真实引擎的解释器里子进程运行
（§8.5 桥）。v2 自行构造伪 `renpy` 包，因此：

- 默认**进程内**解码（`_decompile_in_process`），只需 Python ≥ 3.9；
- 仅当调用方显式传 `python_exe=`（如要用游戏自带解释器）时才走子进程，
  此时仍须给出 `sdk_root=`；
- `runner.py` 的 `--sdk-root` 因此改为**选填**。

### 限制与已知问题

- v2 支持 Ren'Py 8.x，向下至 6.18.0（低于 6.99.10 需上游的 `--no-init-offset`）。
  **Ren'Py 7 / 6.x 及更早的游戏不在支持范围**：那些需上游 `legacy` 分支的 v1.x
  （Python 2.7），本项目未 vendored。
- 要求 Python ≥ 3.9。
- 已知遗留 1 例：SDK launcher 的 `testcases.rpyc` 抛
  `AttributeError: 'Testcase' object has no attribute 'label'`，属上游问题，
  不影响真实游戏脚本样本。
- 中间文件落在 `dst.parent`（已存在的可写目录）以避免新建目录；不可写时回退
  `tempfile.mkdtemp()`，可用 `UNRPYC_WORK_DIR` 覆盖其根。

## 许可与署名保留

- `rpatool.py` 头注释标注其源自 Shizmob 的公共域 RPA-3.0 参考实现，并保留原格式
  说明。
- `unrpyc_real/` 连同其 `LICENSE`（MIT）与作者署名一并提交。上游各文件头自带的
  版权声明（Yuri K. Schlesner、CensoredUsername、Jackmcbarn）保持原样未改，不得删改；本 README 与 `LICENSE` 末尾记录来源 branch / tag / commit。
- v2.0.4 为**上游原样引入**，无本地补丁。

## 升级

上游更新时手动同步，并在本文件与 `vendor/unrpyc_real/LICENSE` 中更新 branch /
tag / commit hash。同步步骤：

1. 确认目标版本线：Ren'Py 8.x 用 `master`（v2.x，Python ≥ 3.9）；Ren'Py 7/6 用
   `legacy`（v1.x，Python 2.7）。二者不可混用。
2. 取上游 `unrpyc.py`、`deobfuscate.py`、`decompiler/*.py`、`LICENSE`，覆盖到
   `vendor/unrpyc_real/`（**勿覆盖本项目自有的 `__init__.py` 与 `runner.py`**）。
3. 核对 API 变化后调整 `__init__.py` 的 `decompile_file()`  shim（v2 的签名为
   `decompile_rpyc(filename: Path, context, ...)`，错误同时写入 `context.error`）。
