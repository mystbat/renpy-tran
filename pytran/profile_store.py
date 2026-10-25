"""TermProfile 的落盘层：原子写 + 用户可读镜像（2026-10-25 数据丢失事故后续）。

背景
----
``profile.json`` 长期是**术语表、免翻短语、翻译规则**的唯一副本。
2026-10-25 的事故里它被 ``unlink`` 掉：118 条术语靠 ``glossary.csv`` 镜像
救了回来，而用户手写的 ``system_append`` 翻译规则没有镜像，**永久丢失**。
单文件单副本是这次损失的根因，不是「手滑」。

本模块把「术语表 CSV 镜像」的做法推广到 profile 的全部字段：

===================  ==================  ==================================
字段                 主文件               镜像（用户可读、可手工编辑）
===================  ==================  ==================================
``terms``           profile.json        glossary.csv（原文,译名）
``exempt_english``  profile.json        exempt.txt（一行一条）
``prompt_overrides`` profile.json       rules.txt（正文 = system_append）
===================  ==================  ==================================

读取语义（与 ``merge_glossary_into_profile`` 保持一致，只有一条规则）
------------------------------------------------------------------
**镜像中非空的条目覆盖主文件；镜像为空/缺失视为「无信息」，不清空主文件。**

* 正常路径下两者永远同步（每次保存都先写镜像、再原子替换主文件），
  所以「覆盖」不会改变任何东西；
* 主文件丢失/损坏时，镜像把用户资产原样复活；
* 镜像为空时（用户确实把规则清空了）不会拿空值去覆盖主文件，
  避免把「文件没写全」误判成「用户想清空」。

写入语义
--------
``save_profile`` 先写镜像，再 ``profile.save()`` 到 ``profile.json.tmp``，
最后 ``os.replace`` 原子替换，并保留上一版 ``profile.json.bak``。
这样即使写到一半断电/被杀，磁盘上至少有一份完整数据（旧文件或旧镜像）。

本模块**不依赖 PyQt6**，可无头测试。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from pytran.config import TermProfile
from pytran.terms import glossary_path, merge_glossary_into_profile, write_glossary_csv

__all__ = [
    "PROFILE_FILENAME",
    "RULES_FILENAME",
    "EXEMPT_FILENAME",
    "profile_path",
    "rules_path",
    "exempt_path",
    "read_rules",
    "write_rules",
    "read_exempt",
    "write_exempt",
    "write_mirrors",
    "save_profile",
    "load_profile",
    "archive_profile_files",
]

PROFILE_FILENAME = "profile.json"
RULES_FILENAME = "rules.txt"
EXEMPT_FILENAME = "exempt.txt"


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def profile_path(work_dir: str | Path) -> Path:
    """``<work_dir>/profile.json``（主文件）。"""
    return Path(work_dir) / PROFILE_FILENAME


def rules_path(work_dir: str | Path) -> Path:
    """``<work_dir>/rules.txt``（翻译规则镜像，正文即 system_append）。"""
    return Path(work_dir) / RULES_FILENAME


def exempt_path(work_dir: str | Path) -> Path:
    """``<work_dir>/exempt.txt``（免翻短语镜像，一行一条）。"""
    return Path(work_dir) / EXEMPT_FILENAME


# --------------------------------------------------------------------------- #
# 翻译规则镜像（rules.txt）
# --------------------------------------------------------------------------- #
def read_rules(work_dir: str | Path) -> str:
    """读 ``rules.txt`` → system_append 文本；缺失/不可读返回 ``""``。

    文件内容就是提示词原文，不做任何解析——用户可以直接用记事本编辑。
    读取时去掉首尾空白（与设置对话框 ``rules_edit.toPlainText().strip()``
    的写入口径一致），否则每轮保存都会累积一个换行。
    """
    path = rules_path(work_dir)
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_rules(append: str, work_dir: str | Path) -> Path:
    """把 system_append 写成 ``rules.txt``，返回路径。

    写 utf-8-sig：Windows 记事本打开中文不乱码（与 glossary.csv 同口径）。
    ``append`` 为空时写出空文件——空文件在读取侧等于「无信息」，
    不会反过来清空 profile.json 里的规则。
    """
    path = rules_path(work_dir)
    path.write_text((append or "").strip(), encoding="utf-8-sig")
    return path


# --------------------------------------------------------------------------- #
# 免翻短语镜像（exempt.txt）
# --------------------------------------------------------------------------- #
def read_exempt(work_dir: str | Path) -> list[str]:
    """读 ``exempt.txt`` → 免翻短语列表；缺失/不可读返回 ``[]``。

    一行一条，忽略空行与 ``#`` 注释行，保序去重。
    """
    path = exempt_path(work_dir)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    items: list[str] = []
    for line in raw.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item not in items:
            items.append(item)
    return items


def write_exempt(items, work_dir: str | Path) -> Path:
    """把免翻短语写成 ``exempt.txt``（一行一条），返回路径。"""
    path = exempt_path(work_dir)
    seen: list[str] = []
    for item in items or []:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    body = "\n".join(seen) + ("\n" if seen else "")
    path.write_text(body, encoding="utf-8-sig")
    return path


# --------------------------------------------------------------------------- #
# 保存 / 读取
# --------------------------------------------------------------------------- #
def write_mirrors(profile: TermProfile, work_dir: str | Path) -> list[Path]:
    """把 profile 的三个字段全部导出为用户可读镜像，返回写出的路径。

    顺序：术语 CSV → 翻译规则 → 免翻短语。任一份写失败都抛 ``OSError``，
    由调用方决定降级（GUI 里只告警，不阻断保存）。
    """
    work = Path(work_dir)
    paths = [write_glossary_csv(profile, work)]
    overrides = profile.prompt_overrides or {}
    paths.append(write_rules(str(overrides.get("system_append", "") or ""), work))
    paths.append(write_exempt(profile.exempt_english or [], work))
    return paths


def save_profile(
    profile: TermProfile, work_dir: str | Path, filename: str = PROFILE_FILENAME
) -> Path:
    """原子保存 profile：先写镜像，再原子替换主文件，并留上一版 ``.bak``。

    * 镜像先写：即使主文件替换失败，用户资产也已经在 CSV/TXT 里；
    * ``os.replace`` 同盘替换是原子的，不会出现「写了一半的 profile.json」；
    * 替换前把旧文件复制成 ``profile.json.bak``，多一层兜底。
    """
    work = Path(work_dir)
    path = work / filename
    write_mirrors(profile, work)

    tmp = path.with_name(path.name + ".tmp")
    try:
        profile.save(tmp)  # 复用 TermProfile 的序列化口径（字段集合唯一来源）
        if path.exists():
            try:
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            except OSError:  # noqa: BLE001 - 备份失败不阻断保存
                pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - 清理失败无害
                pass
    return path


def load_profile(
    work_dir: str | Path, filename: str = PROFILE_FILENAME
) -> tuple[TermProfile, list[str]]:
    """读 profile 并用镜像补齐缺失字段，返回 ``(profile, 提示列表)``。

    提示列表是给人看的中文短句（GUI 写日志、CLI 打 warn），空列表表示
    「一切正常，没有用到任何镜像」。主文件损坏不抛异常——按空 profile
    处理，让镜像有机会把它救回来（历史事故就是这么发生的）。
    """
    work = Path(work_dir)
    notes: list[str] = []

    try:
        profile = TermProfile.load(work / filename)
    except Exception as exc:  # noqa: BLE001 - 损坏 JSON / 权限 / 编码
        profile = TermProfile()
        notes.append(
            f"{filename} 读取失败（{exc}）——已按空 profile 处理，"
            "术语/规则将从镜像文件恢复"
        )

    # terms ← glossary.csv
    try:
        before = len(profile.terms or {})
        merge_glossary_into_profile(profile, work)
        added = len(profile.terms or {}) - before
        if added > 0:
            notes.append(
                f"已从 glossary.csv 恢复 {added} 条术语（{filename} 中缺失）"
            )
    except Exception as exc:  # noqa: BLE001 - CSV 不可读不影响主流程
        notes.append(f"合并 glossary.csv 失败：{exc}")

    # exempt_english ← exempt.txt
    try:
        exempt = read_exempt(work)
        if exempt and list(profile.exempt_english or []) != exempt:
            notes.append(f"已从 exempt.txt 恢复 {len(exempt)} 条免翻短语")
            profile.exempt_english = exempt
    except Exception as exc:  # noqa: BLE001
        notes.append(f"读取 exempt.txt 失败：{exc}")

    # prompt_overrides.system_append ← rules.txt
    try:
        rules = read_rules(work)
        current = str((profile.prompt_overrides or {}).get("system_append", "") or "")
        if rules and rules != current:
            notes.append(
                "已从 rules.txt 恢复翻译规则"
                + ("（profile.json 中的规则为空）" if not current else "（以 rules.txt 为准）")
            )
            overrides = dict(profile.prompt_overrides or {})
            overrides["system_append"] = rules
            profile.prompt_overrides = overrides
    except Exception as exc:  # noqa: BLE001
        notes.append(f"读取 rules.txt 失败：{exc}")

    return profile, notes


# --------------------------------------------------------------------------- #
# 归档（换游戏目录时调用）
# --------------------------------------------------------------------------- #
def archive_profile_files(work_dir: str | Path, *, tag: str) -> list[Path]:
    """把 profile.json 及其全部镜像**移动**到 ``bak/``，返回新路径列表。

    换游戏目录时必须连镜像一起搬走：只搬 profile.json 会留下 glossary.csv /
    rules.txt，下一次 ``load_profile`` 又把旧游戏的术语和规则复活到新游戏上。
    用 ``Path.replace``（同盘 rename）而非删除——用户资产只搬不删。

    ``tag`` 用于文件名后缀，建议传 ``"switched-<YYYYmmdd-HHMMSS>"``：
    与 ``_backup_current_project`` 写出的 ``profile-<ts>.json`` 区分开，
    否则同一秒内先备份再归档会把备份快照覆盖掉。
    """
    work = Path(work_dir)
    bak = work / "bak"
    pairs = [
        (work / PROFILE_FILENAME, "profile", ".json"),
        (glossary_path(work), "glossary", ".csv"),
        (rules_path(work), "rules", ".txt"),
        (exempt_path(work), "exempt", ".txt"),
    ]
    moved: list[Path] = []
    for src, stem, suffix in pairs:
        if not src.exists():
            continue
        bak.mkdir(parents=True, exist_ok=True)
        dst = bak / f"{stem}-{tag}{suffix}"
        try:
            src.replace(dst)
        except OSError:  # noqa: BLE001 - 搬不动就留在原地（宁可重复，不可丢）
            continue
        moved.append(dst)
    return moved
