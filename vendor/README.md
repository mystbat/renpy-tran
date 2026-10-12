# vendor/ — 内置第三方工具源码

根据 ADR-0005，将上游 `unrpyc` 与 `rpatool` 以**明文源码**内置，替代原
`UnRen-forall.bat` 的 base64+cab+PowerShell 运行时释放方式，便于审计、许可合规
与单元测试，并去掉对系统组件 `expand.exe` 的依赖。

## 内容

| 文件 | 来源 | 许可 | 说明 |
| --- | --- | --- | --- |
| `rpatool.py` | Shizmob/rpatool（RPA-3.0 格式公共域参考实现） | 公共域 | 本地化、stdlib-only 重实现的 RPA-3.0 解包/打包，含 `extract`/`create` 两个入口 |
| `unrpyc.py` | 薄封装入口 | MIT | 处理 `tests/fixtures` 合成 `fake_unrpyc` 的 `.rpyc`（内嵌原始 `.rpy`），并可在 `vendor/unrpyc_real/` 放下真实上游 `unrpyc` 时自动委派（见 ADR-0008） |

上游真实 unrpyc（CensoredUsername 原版 + F95Sam 编辑版）体量庞大、依赖复杂，
不在 W4 范围；其插入方式见 ADR-0008 的 drop-in 升级路径：将上游源码放入
`vendor/unrpyc_real/` 并暴露 `decompile_file(src, dst, try_harder=...)` 即可被
本封装自动发现。

## 许可与署名保留

- `rpatool.py` 头注释标注其源自 Shizmob 的公共域 RPA-3.0 参考实现，并保留原格式
  说明。
- 真实上游 unrpyc 放入 `vendor/unrpyc_real/` 时，须连同其 `LICENSE` 与作者署名
  一并提交；合并时需核对（ADR-0005 后果条款）。

## 升级

上游更新时手动同步，并在本文件记录上游 commit hash。
