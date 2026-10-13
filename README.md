# py-tran —— Ren'Py 游戏 AI 汉化工具（3.0）

把英文 Ren'Py 游戏（Galgame / 视觉小说）里的对话和字幕，批量翻译成简体中文。
**你不需要会编程**，按下面的步骤一步步点就行。

> ⚠️ 免责声明：本工具「概不负责」。翻译结果由 AI 生成，可能存在错译、漏译或
> 风格不符；请自行核对。本工具不附带任何游戏本体，也不提供任何游戏下载。
> 是否使用本工具、如何使用，以及由此产生的一切后果，均由使用者自行承担。

---

## 一、它能做什么

1. **一键汉化**：选好游戏目录，点一下就开始（解包 → 反编译 → 翻译 → 导出）。
2. **旧工程迁移**：如果你用的是 2.0 旧版翻译成果，可以用本工具迁移进 3.0 工程继续用。
3. **人工通道**：导出成 CSV 表格，人工改完再导回；还能逐条审校、接受、标记。
4. **安全**：你的翻译 API Key 只存在你电脑的环境变量里，永远不会写进配置或打包文件。

---

## 二、使用前准备

### 1. 安装 Python（如果还没装）
- 去 [python.org](https://www.python.org/downloads/) 下载并安装 **Python 3.10 或更高版本**。
- 安装时记得勾选「**Add Python to PATH**」（添加到环境变量）。

### 2. 安装本工具需要的库
打开「命令提示符」或「PowerShell」，进入本工具目录后运行：

```bat
pip install -r requirements.txt
```

> 只会装两个库：`openai`（调用翻译接口）和 `pytest`（仅开发者测试用，可不选）。

### 3. 填好你的翻译 API Key
本工具需要一个**翻译接口 Key**（比如硅基流动 SiliconFlow 的 Key）。
**不要**把 Key 写进 `config.ini`。

正确做法——把它放进**环境变量** `TL_API_KEY`：

- Windows（PowerShell，临时）：
  ```powershell
  $env:TL_API_KEY = "你的Key"
  ```
- 想永久生效：系统设置 → 高级 → 环境变量 → 新建用户变量
  `TL_API_KEY` = `你的Key`。

### 4. 配置（config.ini）
把 `config.example.ini` 复制一份，改名成 `config.ini`，按需填写：

- `[API] url`：翻译接口地址（样例已填好硅基流动，一般不用改）。
- `[Model] model`：填写你想用的模型名（问你的 Key 提供方）。
- `[Game] renpy_sdk_dir`：Ren'Py SDK 目录（含 `renpy.exe`）。**纯文件翻译可留空**。

> 务必保留 `[API] api_key =` 这一行为空。真实 Key 只走环境变量（见上）。

---

## 三、开始汉化（纯文件模式，最简）

如果你只想翻译一组 `.rpy` 脚本文件：

```bat
python -m pytran.cli translate 游戏脚本1.rpy 游戏脚本2.rpy --work-dir 我的工程
```

它会把译文写到 `我的工程/output/` 目录，不会改动你的游戏本体。

---

## 四、汉化整个游戏（一键流程）

```bat
python -m pytran.cli game "游戏安装目录" --level 1
```

工具会自动完成：解包（如需要）→ 反编译 `.rpyc` → 生成翻译骨架 → 调用 AI 翻译 → 导出。
完成后在 `游戏安装目录/pytran_work/output/` 里查看译文。

> 一键流程里「直接写回游戏」属于最高等级（`--level 2`），必须额外确认（命令行加 `--yes`），
> 建议先用 `--level 1` 只导出核对，确认无误再升级到写回。

---

## 五、人工校对（可选但推荐）

1. 导出 CSV 表格：
   ```bat
   python -m pytran.cli export-csv --work-dir 我的工程
   ```
   在 `我的工程/output/translation.csv` 里用 Excel / 记事本逐条修改「译文」列。
2. 导回：
   ```bat
   python -m pytran.cli import-csv output/translation.csv --work-dir 我的工程
   ```
3. 也可以启动图形审校界面（需要电脑有显示器，Windows 下直接双击运行）：
   ```bat
   python -m pytran.cli ui --work-dir 我的工程
   ```
   在界面里可以：上/下移动、通过（a）、标记失败（f）、保存（Ctrl+S）。

> 任何「全部接受」操作都会先自动备份到 `logs/`，随时可以撤销。

---

## 六、从 2.0 旧工程迁移

把旧版 2.0 的翻译产物（包含 `rpybak/` 与 `split_csv/bak/`）放到一个目录后：

```bat
python -m pytran.cli migrate "旧2.0目录" --work-dir 新3.0工程
```

迁移会按「块序 + 行序」把旧译文回填进 3.0 工程（`project.json`）。
匹配率低于 95% 时会自动拦下并提示，未对上的行会被标记为 `failed` 并注明原因，方便你人工补译。

---

## 七、常见问题

- **翻译不动 / 报错「缺 Key」**：检查 `TL_API_KEY` 环境变量是否设置，且 `config.ini` 里
  `api_key` 留空。
- **中文乱码**：CSV 用「UTF-8 带 BOM」编码，用 Excel 打开一般正常；若乱码请改用
  VS Code / 记事本另存为 UTF-8。
- **反编译失败**：检查 `[Game] renpy_sdk_dir` 是否指向正确的 Ren'Py SDK（含 `renpy.exe`）。
- **想从头重来**：删掉 `output/` 和工程目录里的 `project.json` 即可，游戏本体不受影响。

---

## 八、目录速览

| 路径 | 作用 |
|---|---|
| `pytran/` | 程序核心代码 |
| `config.example.ini` | 配置样例（复制为 `config.ini` 后填写） |
| `requirements.txt` | 依赖清单 |
| `scripts/` | 辅助脚本（如清理 git 历史的 PowerShell 脚本） |
| `docs/` | 设计文档与每个工作包的说明 |

---

## 九、再次声明

本工具仅供学习与个人备份用途，**概不负责**任何翻译质量、版权、或由此引发的问题。
请遵守你所在地区关于游戏本地化与 AI 使用的相关法规，尊重原作者的知识产权。
