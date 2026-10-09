# archive/ — 旧版本归档

本目录存放 3.0 重构前的全部旧版内容。**此目录为只读归档，不再维护**；
3.0 的代码在 `pytran/`，文档在 `docs/`。

| 目录 | 内容 |
|---|---|
| `1.x/code/` | 第一代工作脚本（tocsv/split_csv/work_csv/fixcsv/output/torpy/menu 等） |
| `1.x/data/` | 1.x 的配置、提示词、消息文件（**其中 config.ini 含已泄露的旧 API Key，已废弃，待吊销**） |
| `package/` | 原发行包：1.0、2.0（含嵌入式 Python 3.13.5 与 UI 版）、bak、素材 |
| `workspace/` | 历次翻译运行留下的工作数据（csv/split_csv/pending/text 等） |

来源映射：根目录脚本与数据 → `1.x/`；`package/` 整体迁入；各数据目录 → `workspace/`。
迁移时间：2026-10-09（3.0 T0 之后，commit 见 git log）。
