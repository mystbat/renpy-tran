# BUGFIX：切换游戏目录静默清空工程（2026-10-25 数据丢失事故）

## 一、事故现象

用户在真实应用（`运行.bat` → `--work-dir "%~dp0."`，即仓库根）里点了一次
「选择游戏目录」，随后发现：

| 文件 | 事故前 | 事故后 |
| --- | --- | --- |
| `project.json` | 12,099 段 / 16 文件（8.1 MB） | 356 字节空壳（只有 `game_root`） |
| `profile.json` | 118 条术语 + 翻译规则 | **文件不存在** |
| `glossary.csv` | 118 条 | 未受影响（因为不在删除名单里） |

12,068 段译文与全部术语表、翻译规则一次性消失，且 `profile.json` **全盘无副本**：
git 未跟踪（`.gitignore` 忽略）、`bak/` 里没有、回收站没有、卷影副本无权限读取、
PyCharm LocalHistory 只覆盖 2025 年数据。

## 二、根因

`pytran/ui.py` 旧 `_choose_directory()`：

```python
if game_dir:
    if self.project.segments or self.project.game_root != game_dir:
        self.project = Project(version=3, app="pytran", game_root=game_dir)
        self.state = type(self.state)(self.project)
        try:
            (self.work_dir / "profile.json").unlink(missing_ok=True)   # ← 元凶
            (self.work_dir / "project.json").unlink(missing_ok=True)   # ← 元凶
        except OSError:
            pass
        self._log("已切换游戏目录，旧导入数据与术语表已清空")
    self.project.game_root = game_dir
    JsonProjectStore().save(self.project, self.work_dir)               # 写出 356 字节空壳
```

四个致命点：

1. **无确认**：选目录即执行，用户没有「取消」的机会；
2. **无备份**：`unlink` 之前没有任何快照（`_backup_current_project()` 写好了但**从未被调用**）；
3. **删除范围过宽**：`profile.json` 是**用户手工维护的资产**（术语表 + 翻译规则），
   无法从游戏目录重建，却和可重建的 `project.json` 一起被删；
4. **`except OSError: pass`**：删除失败静默吞掉，日志仍宣称「已清空」。

判定条件 `self.project.segments or ...` 也有问题：只要**当前有段**就触发重置，
即使新旧目录是同一个（用户重选原目录也会清空）。

## 三、修复

### 3.1 `_choose_directory()` 重写（`pytran/ui.py`）

```
选中目录
  ├─ 规范化路径比较（normcase + abspath）
  ├─ 无切换（同一个目录）→ 只更新 game_root，绝不触碰数据
  ├─ 有切换 → QMessageBox 三要素确认（原目录 / 新目录 / 将清空多少段）
  │    ├─ 用户选「否」→ 立即 return，工程完全不变
  │    └─ 用户选「是」→ _backup_current_project()（先备份）
  │         ├─ project.json → bak/project-<ts>.json
  │         └─ profile.json → bak/profile-<ts>.json
  │         → 重建空 Project → profile.json **移入** bak/profile-switched-<ts>.json
  └─ 保存新工程
```

关键改动：

* 加确认对话框，默认按钮为「否」（误按回车不会删数据）；
* 先备份再重置，备份路径写进日志；
* `profile.json` 不再 `unlink`，改为 `Path.replace()` 移入 `bak/`——术语表是资产，必须可找回；
* 删掉 `except OSError: pass`，移存失败时告警并保留原文件；
* 用规范化路径判等，重选同一目录变成真正的 no-op。

### 3.2 `_backup_current_project()` 扩展

原来只备份 `project.json`；现在同时备份 `profile.json`（术语表 + 翻译规则）。
两者都不存在时不创建 `bak/`（保持「无内容可备份」语义）。

### 3.3 `_load_profile()` 增加 CSV 兜底

`glossary.csv` 是每次保存术语表时同步导出的用户可读镜像，事故中幸存。
现在 `_load_profile()` 在读取 `profile.json` 后调用
`merge_glossary_into_profile(profile, work_dir)`：

* 同名条目以 CSV 为准（用户可能手改过 CSV）；
* CSV 新增条目追加；
* profile 独有条目保留（不清空）。

效果：即使 `profile.json` 再次丢失，118 条术语也能从 `glossary.csv` 自动复活。

### 3.4 测试隔离加固（`tests/conftest.py`）

事故另一层风险：**测试**也会写到仓库根。7 个 GUI 测试曾用
`PytranApp(proj, work_dir=".")`——在仓库里跑 pytest 就等于拿用户真实工程做实验。

新增 autouse fixture `_forbid_writes_into_repo_root`：

* 拦截 `JsonProjectStore.save`，`work_dir` 解析为仓库根 → `AssertionError`；
* 拦截 `TermProfile.save`，父目录为仓库根 → `AssertionError`；
* 拦截 `PytranApp.__init__`，`work_dir` kwarg 为仓库根 → `AssertionError`。

7 处 `work_dir="."` 全部改为 `tmp_path`。

## 四、数据恢复

`profile.json` 与 `project.json` 已重建，`glossary.csv` / `config.ini` 从未受损。

| 项 | 恢复方式 | 结果 |
| --- | --- | --- |
| `project.json` | 用 `RenpyParser` 重新解析 `game/tl/chinese2/**/*.rpy` 16 个文件，`build_project()` + `JsonProjectStore.save()` | 12,099 段 / 16 文件 |
| 段状态 | 从磁盘文件反推（有译文 → `TRANSLATED`，SDK 空模板 → `PENDING`） | pending 26 / translated 12,068 / skipped 5 |
| `profile.json` | `TermProfile(game="HOROS")` + `merge_glossary_into_profile()` | 118 条术语 |
| `imported_at` | 用磁盘文件 mtime 回填 | 16 个文件 |

**回写保真度校验**：对每个文件 `parse_file()` → `regenerate()`，与磁盘字节比对
（剥 BOM 与尾部换行）→ **16/16 内容完全一致**。即重建后的工程写回游戏目录
不会产生任何内容变化。

期间发现 3 行译文与磁盘不一致（`quest.rpy` 1 行、`exp-wos.rpy` 2 行，
用户在事故后手工改过 `.rpy`），已按磁盘为准同步回工程。

## 五、无法恢复的部分

**`profile.json` 里的翻译规则（`prompt_overrides["system_append"]`）永久丢失。**
排查范围：git 全历史、`git stash`、`bak/`、`output/`、`.worktrees/*`、`logs/`、
回收站、PyCharm LocalHistory、全盘 `*.json` 扫描——均无副本。

同样丢失的还有 `exempt_english`（免翻英文短语白名单），当前为 `[]`。

**这两项需要用户重新填写**（设置 → 翻译规则 / 术语表页的豁免框）。

## 六、回归测试

新增 `tests/test_ui_directory_switch.py`（6 个用例）：

| 用例 | 锁定行为 |
| --- | --- |
| `test_cancel_keeps_project_and_profile` | 选「否」→ 工程/磁盘分毫不动，不产生 `bak/` |
| `test_confirm_backs_up_before_clearing` | 选「是」→ 先备份，旧 `profile.json` 移入 `bak/`，备份内术语与规则完好 |
| `test_same_directory_does_not_touch_data` | 重选同目录 → no-op |
| `test_load_profile_recovers_terms_from_glossary_csv` | `profile.json` 缺失 → 术语从 CSV 复活 |
| `test_load_profile_merges_csv_without_dropping_profile_only_terms` | 合并时不丢 profile 独有条目 |
| `test_conftest_guard_blocks_repo_root_writes` | 仓库根守卫生效 |

全量：`526 passed`（原 520 + 新增 6），且跑完后仓库根
`project.json` / `profile.json` / `glossary.csv` / `config.ini` 的 mtime 与大小不变。

## 七、遗留与后续建议

1. **`_clear_project()` 同类风险**：它保留了 `game_root`、不删 `profile.json`，
   但同样**不备份**就覆写 `project.json`。建议后续补一次 `_backup_current_project()`。
2. **导入/清空/写回三条路径都应留底**：目前只有切换目录会备份。
   建议把「写 `project.json` 前自动快照（保留最近 N 份）」做成 `JsonProjectStore` 层面的统一策略。
3. **`运行.bat` 的 `--work-dir` 指向仓库根**：这让「代码仓库」与「用户数据目录」耦合，
   任何在仓库里跑的脚本都可能碰到真实数据。长期看应把工作区挪到独立目录。

## 八、后续修复：用户资产持久化落盘（2026-10-25 完成）

§五「无法恢复的部分」的根因不是 `unlink` 那一行，而是**单文件单副本**：
`glossary.csv` 有独立镜像，所以 118 条术语活了；`system_append` 和
`exempt_english` 没有镜像，所以全灭。

已按「给每个字段都配镜像」修复，详见 **`docs/FEATURE-profile-mirror.md`**：

| 字段 | 镜像 |
| --- | --- |
| `terms` | `glossary.csv`（既有） |
| `prompt_overrides["system_append"]` | `rules.txt`（新增） |
| `exempt_english` | `exempt.txt`（新增） |

`pytran/profile_store.py` 统一负责「先写镜像 → 原子替换主文件 → 留 `.bak`」，
`load_profile` 在主文件丢失/损坏时从镜像逐段恢复。
`_choose_directory()` 现在连三个镜像一起移入 `bak/`（此前只搬 `profile.json`，
残留的 `glossary.csv` 会把旧游戏术语复活到新游戏上——这是本次一并修掉的次生缺陷）。

§五 中丢失的两项内容仍需用户重新填写，但现在**再丢也丢不了**：
即便 `profile.json` 被删，`rules.txt` / `exempt.txt` 也能原样恢复。

