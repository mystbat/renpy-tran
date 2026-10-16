# ADR-0008：unrpyc 集成采用「薄封装 + drop-in 真实上游」策略

- 日期：2026-10-09
- 状态：已接受（已落地。vendored 上游经 v2.0.4 升级后不再需要子进程桥，见下方「2026-02 修订」）
- 影响范围：W4 `vendor/unrpyc.py`、`pytran/unren.py` 反编译入口（§8.1 ②、§8.2）

## 背景

设计文档 §8 要求内置 `unrpyc`（CensoredUsername 原版 + F95Sam 编辑版）以替代
`UnRen-forall.bat` 的 base64+cab 运行时释放（ADR-0005 已确立「vendor 源码内置」大方向）。
但真实上游 unrpyc 体量庞大、依赖复杂（需匹配目标游戏的解释器版本、含多进程编辑版逻辑），
不在 W4 工作量内；而 W4 必须给出可单测、可落地的反编译入口与降级路径。

## 决策

`vendor/unrpyc.py` 实现为一个**薄封装入口**，而非直接内置完整上游：

1. 处理 `tests/fixtures/fake_game` 中的**合成** `.rpyc`（内嵌原始 `.rpy`，W4 自造样本，
   不含真实 Ren'Py 字节码），提供 `decompile_file` / `decompile_files` 入口，使 `UnrenImpl.decompile`
   在 CI/单测中即可运行；
2. 预留 `vendor/unrpyc_real/` 目录：真实上游 unrpyc 源码放入该目录并暴露
   `decompile_file(src, dst, try_harder=...)` 时，薄封装**自动委派**给真实版（drop-in 升级）；
3. 默认 `--try-harder` 参数透传到 `decompile_files`，保持与 §8.1 ②「混淆游戏反编译」一致；
4. 无真实版且目标非合成样本时，反编译返回空并记 warning，由上层（解包/写回流程）按
   §8.3 提示用户配置匹配解释器，不硬崩。

配套：`vendor/README.md` 记录来源、许可与 drop-in 升级步骤；真实上游放入须连同其
`LICENSE` 与作者署名（ADR-0005 后果条款）。

## 理由

- W4 时间盒内无法审计/内置完整上游 unrpyc，但「反编译入口必须存在且可测」是硬需求；
  薄封装 + 合成样本让 **29 项 W4 测试在零外部依赖下全绿**；
- drop-in 设计让后续 W6/维护者无需改 `pytran/` 代码即可启用真实 unrpyc：放入目录即生效，
  满足「上游更新时手动同步」且不影响既有契约（`pytran/interfaces.py` 中 `Unren` 签名不变）；
- 拒绝「W4 直接 vendored 完整上游」：会引入不可审计的庞大依赖与版本耦合，违背 §8.2 的
  可审计/可测试目标，且合成样本足以覆盖反编译调用链与 write-back 衔接。

## 后果

- `vendor/__init__.py` 暴露 `vendor.unrpyc` / `vendor.rpatool`，`pytran/unren.py` 以
  `from vendor import unrpyc` 调用，契约稳定；
- 真实游戏（非合成 rpyc）的反编译覆盖率取决于用户是否放置 `vendor/unrpyc_real/`；
  交付说明 §15.5 第 3 点已说明内置骨架覆盖率有限，需真实 SDK/unrpyc 才达 100%；
- 升级真实上游时只需核对 LICENSE 与上游 commit hash 记入 `vendor/README.md`，不改 ADR。

---

## 2026-02 修订：vendored 上游升级至 Unrpyc v2.0.4

### 背景

W4b 落地时 vendored 的是上游 **2019-10-07 的 `44febb0`**（Ren'Py 7.x 时代）。
在 Ren'Py 8.3.7 上暴露出三个已确认缺陷：

1. **RPYC2 多 slot 处理错误。** `read_ast_from_file()` 把整个 slot **字典**
   喂给 `zlib`（`codecs.decode(chunks, 'zlib')`）。按分支选择条件的不同，表现为
   `TypeError: a bytes-like object is required, not 'dict'`，或——当引擎自身的
   `read_rpyc_data` 已消耗部分流时——表现为用户实际看到的
   `Error -5 while decompressing data: incomplete or truncated stream`。
2. **`--try-harder` 在 Python 3 上 100% 失败。** `deobfuscate.py` 残留 Python 2 写法
   （`e.message`、`ord()` 作用于 int、int 与 str 比较），37 个样本全部崩溃，
   而交付说明 §⑦ 将其标为「✅ 支持」。
3. **不支持 8.x AST。** 2019 代码不认识 8.x 的节点布局，SDK launcher 样本 37 个
   中有 22 个抛 `AttributeError`（`Label` 无 `name`、`Signature` 无 `extrapos`、
   `Style` 无 `take`、`PyExpr` 无 `linenumber` 等）。

### 修订决策

将 `vendor/unrpyc_real/` 的上游替换为 **CensoredUsername/unrpyc `master` 分支的
`v2.0.4` 标签**（commit `3ae8334ed71a05535927dcc559663d3aca51215b`），
取自上游仓库，非 fork。v2 明确 **只含 Python 3 实现**，专攻 Ren'Py 8.x
（向下兼容到 6.18.0），与本项目的 3.13 工具链匹配。
Ren'Py 7 及更早的游戏不在 v2 支持范围内（需上游 `legacy` 分支的 v1.x，未 vendored）。

### 由此带来的架构变化：子进程桥不再是必需项

这是本次修订最重要的一点。上游 v2 **不再 import 真实的 `renpy` 引擎**：
`decompiler/renpycompat.py` 通过 `magic.fake_package("renpy")` 自建伪 `renpy`
包，并用自己的兼容类从 pickle 流重建 AST。因此：

- `decompile_file()` 可**直接在本项目解释器内运行**（仅要求 Python ≥ 3.9），
  不再需要 §8.5 的子进程桥与真实的 Ren'Py SDK/解释器；
- `_decompile_in_process()` 成为默认路径；**仅当调用方显式传入 `python_exe`**
  （例如要用游戏自带的解释器解码）时才走子进程，且此时仍要求给出 `sdk_root`；
- `runner.py` 的 `--sdk-root` 由必填改为选填，仅为兼容性保留。

该变化同时消除了此前「游戏自带解释器 + 引擎路径」的部署耦合：过去若探测失败
（或沙箱禁写 temp）即整链失败，如今不依赖外部解释器。

### 验证结果

| 样本 | 旧 44febb0 | 新 v2.0.4 |
| --- | --- | --- |
| 用户真实游戏 HOROS-pc（20 个 .rpyc） | 走错分支时 0/20 | **20/20**（1,918,420 字符） |
| Ren'Py SDK launcher（37 个 .rpyc） | 15/37 | **36/37**（余 1 个 `testcases.rpyc`，上游 `Testcase.label` 已知问题） |
| `--try-harder` | 0/37 全崩 | 全部可用 |

### 兼容性 / 部署说明

- v2 要求 **Python ≥ 3.9**；`_require_supported_python()` 会在旧解释器上给出明确报错。
- 解码需要临时落地中间文件。优先考虑在 `dst.parent`（已存在的可写目录）就地暂存，**不新建目录**，
  以适配「允许写文件但禁止 mkdir」的沙箱环境；不可写时才回退到
  `tempfile.mkdtemp()`，并可用环境变量 `UNRPYC_WORK_DIR` 覆盖其根目录
  （该变量此前只存在于注释中、并未实现，现已落实）。
- Ren'Py 7 / 6.x 游戏请用上游 `legacy` 分支的 v1.x，本项目不 vendored。
