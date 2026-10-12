# ADR-0008：unrpyc 集成采用「薄封装 + drop-in 真实上游」策略

- 日期：2026-10-09
- 状态：临时策略（总控 2026-10-09 审核：合成样本仅覆盖调用链, 真实游戏反编译为零覆盖；已立 W4b 强制内置真实 unrpyc, 本 ADR 于 W4b 交付后复审）
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
