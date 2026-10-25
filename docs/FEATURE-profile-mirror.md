# 用户资产持久化落盘（profile 镜像机制）

状态：✅ 已实现（2026-10-25）
相关代码：`pytran/profile_store.py`（新增）、`pytran/terms.py`、`pytran/ui.py`、`pytran/cli.py`
相关事故：`docs/BUGFIX-data-loss-directory-switch.md`、`docs/W5-交付说明.md` §七

---

## 一、为什么要有这层东西

2026-10-25 换游戏目录时，`_choose_directory()` 把 `profile.json` 直接
`unlink` 掉了。后果分两种：

| 用户资产 | 事故结局 | 原因 |
|---|---|---|
| 术语表 118 条 | **自动复活** | `glossary.csv` 是一份独立镜像 |
| 免翻短语 | 丢失（需手工重录） | 只在 `profile.json` 里 |
| 翻译规则 `system_append` | **永久丢失** | 只在 `profile.json` 里，用户手写的长文本 |

结论：**单文件单副本是根因，不是「手滑」**。已经有镜像的字段活了下来，
没有镜像的字段全灭。所以修法不是「以后小心点」，而是给每个字段都配镜像。

---

## 二、落盘布局

`work_dir` 下（真实应用即仓库根，见 `运行.bat` 的 `--work-dir "%~dp0."`）：

```
profile.json     主文件（JSON，程序读写，含全部字段）
profile.json.tmp 写入中间态（写完即 os.replace 掉，正常情况下不残留）
profile.json.bak 上一版快照（每次保存前 copy2）
glossary.csv     术语表镜像    原文,译名
rules.txt        翻译规则镜像  正文 = system_append
exempt.txt       免翻短语镜像  一行一条
```

三个镜像都是 **UTF-8 带 BOM**（`utf-8-sig`）：Windows 记事本直接打开不乱码。
全部在 `.gitignore` 的「用户数据」段里，不进版本库。

| 字段 | 主文件 | 镜像 | 格式 |
|---|---|---|---|
| `terms` | `profile.json` | `glossary.csv` | CSV，表头 `原文,译名` |
| `prompt_overrides["system_append"]` | `profile.json` | `rules.txt` | 纯文本正文 |
| `exempt_english` | `profile.json` | `exempt.txt` | 一行一条 |

---

## 三、读取语义（只有一条规则）

> **镜像里非空的条目覆盖主文件；镜像为空或缺失视为「无信息」，绝不清空主文件。**

这条规则是刻意选的反直觉方向，理由是数据丢失的代价远高于「清空不方便」：

* 正常路径下两者永远同步（每次保存都先写镜像、再原子替换主文件），
  所以「覆盖」不改变任何东西；
* 主文件丢失/损坏 → 镜像把用户资产原样复活；
* 镜像为空（用户确实想清空规则）→ **不会**拿空值覆盖主文件。
  代价是「不能靠清空镜像来清规则」，但这个操作本来就有更安全的入口：
  设置对话框的「防误清空」守卫（见下）。

如果两者不一致（用户用记事本改了 `rules.txt`），**以镜像为准**——
用户手改镜像的意图比程序上次写的 `profile.json` 更新。

## 四、写入语义

`save_profile(profile, work_dir)` 的顺序是刻意安排的：

1. 先写三个镜像（`glossary.csv` → `rules.txt` → `exempt.txt`）；
2. `profile.save(profile.json.tmp)`；
3. `shutil.copy2` 现有 `profile.json` → `profile.json.bak`；
4. `os.replace(tmp, profile.json)` 原子替换；
5. `finally` 里清掉可能残留的 tmp。

任何一步中断，磁盘上都至少有一份完整数据（旧主文件或新镜像）。
镜像先写是为了让「新数据先落地」——镜像本身就是最耐用的那份。

## 五、读取入口

| 入口 | 用途 |
|---|---|
| `profile_store.load_profile(work_dir)` | GUI / CLI 统一入口，返回 `(profile, notes)` |
| `cli.load_profile_with_mirrors(work_dir)` | CLI 三个子命令用，notes 打到 `logger.warning` |

`load_profile` 的四段恢复流程，每段单独 `try/except`（一段失败不影响其余）：

1. `TermProfile.load` → 失败则空 profile + note
   `profile.json 读取失败（…）——已按空 profile 处理，术语/规则将从镜像文件恢复`
2. `merge_glossary_into_profile` → note `已从 glossary.csv 恢复 N 条术语（profile.json 中缺失）`
3. `exempt.txt` → note `已从 exempt.txt 恢复 N 条免翻短语`
4. `rules.txt` → note `已从 rules.txt 恢复翻译规则（profile.json 中的规则为空）`
   或 `（以 rules.txt 为准）`

主文件不存在且 notes 为空 = 全新工程，只记 `logger.info`，不报警
（否则每个新用户第一次启动就看到四条「恢复」警告）。

## 六、换目录 / 清空工程时的处理

两个地方必须连镜像一起搬，否则旧游戏的术语和规则会复活到新游戏上
（`load_profile` 会忠实地把残留的 `glossary.csv` 合并进来）：

* `_choose_directory()` → `profile_store.archive_profile_files(work_dir, tag=f"switched-{ts}")`
  → 四个文件 `Path.replace` 到 `bak/profile-switched-<ts>.json`、
  `glossary-switched-<ts>.csv`、`rules-switched-<ts>.txt`、`exempt-switched-<ts>.txt`。
  `switched-` 前缀用于与 `_backup_current_project()` 刚写出的
  `profile-<ts>.json` 区分，避免同一秒内互相覆盖。
* `_backup_current_project()` → `shutil.copy2` 四个文件到 `bak/*-<ts>.*`（拷贝，不移动）。

两者都**只搬/只拷不删**。文件不存在时不创建空的 `bak/`。

## 七、设置界面

**Tab 2 术语表**：导出 CSV / 导入 CSV / 打开位置
（`write_glossary_csv_rows` / `read_glossary_csv`）。

**Tab 3 翻译规则**：导出到 rules.txt / 从 rules.txt 载入 / 打开位置。
导出按钮不依赖「保存」——用户想立刻留底就能立刻落盘。

点「保存」后的汇总弹窗会明确列出三个镜像文件名 + 目录，并说明
「丢失 profile.json 时可据此自动恢复」。同时保留两条防误清空守卫：
表格被清空而原有术语非空、规则框被清空而原有规则非空，都会弹警告并
跳过写入，不会拿空值覆盖。

## 八、测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_profile_store.py`（21） | 镜像往返、BOM、去重、注释行、原子性、四段恢复、空镜像不清空、镜像覆盖主文件、归档 |
| `tests/test_glossary_csv.py`（8） | 既有 CSV 行为 |
| `tests/test_ui_settings_dialog.py` | 保存后生成 `rules.txt` / `glossary.csv`；删掉 `profile.json` 后规则从 `rules.txt` 复活；导出按钮即时落盘 |
| `tests/test_ui_directory_switch.py` | 换目录时四个文件全部移入 `bak/`，新游戏读到空 profile |

`tests/conftest.py` 的防污染守卫现在也拦 `profile_store.save_profile` /
`write_mirrors`——它们直接 `path.write_text`，绕过了 `TermProfile.save`
原有的守卫，否则一个 `work_dir="."` 的测试就能覆盖用户的真实术语表。

## 九、已知边界

* `rules.txt` 只能存 `system_append`。`prompt_overrides` 若将来加了别的键，
  需要再配镜像。
* 镜像为空不覆盖主文件 ⇒ 无法靠「清空 rules.txt」来清规则，得走设置界面。
* `profile.json.bak` 只保留上一版（单份），不是版本历史；需要更多历史用 `bak/`。
