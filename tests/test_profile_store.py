"""profile 持久化层测试（2026-10-25 数据丢失事故回归，无头，不依赖 PyQt6）。

背景
----
事故里 ``profile.json`` 被删，118 条术语靠 ``glossary.csv`` 复活，
而用户手写的翻译规则（``prompt_overrides["system_append"]``）与免翻短语
（``exempt_english``）**没有任何磁盘副本**，永久丢失。

本模块锁定 ``pytran.profile_store`` 的行为契约：

* 三项资产都有用户可读镜像（glossary.csv / rules.txt / exempt.txt）；
* 主文件丢失/损坏时，镜像能把它们原样复活；
* 镜像为空 ≠ 清空主文件（避免「文件没写全」被当成「用户想清空」）；
* 保存是原子的：中途失败不会留下半个 profile.json，且留 .bak；
* 换游戏目录时镜像必须一起搬走，否则旧游戏的术语会污染新游戏。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pytran.config import TermProfile
from pytran.profile_store import (
    EXEMPT_FILENAME,
    RULES_FILENAME,
    archive_profile_files,
    exempt_path,
    load_profile,
    profile_path,
    read_exempt,
    read_rules,
    rules_path,
    save_profile,
    write_exempt,
    write_mirrors,
    write_rules,
)


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    return d


def _full_profile() -> TermProfile:
    return TermProfile(
        terms={"Alice": "爱丽丝", "Bob": "鲍勃"},
        exempt_english=["OK", "Yes"],
        prompt_overrides={"system_append": "保持人名不译，语气口语化。"},
    )


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def test_paths_are_in_work_dir(work_dir: Path) -> None:
    assert profile_path(work_dir) == work_dir / "profile.json"
    assert rules_path(work_dir) == work_dir / RULES_FILENAME == work_dir / "rules.txt"
    assert exempt_path(work_dir) == work_dir / EXEMPT_FILENAME == work_dir / "exempt.txt"


# --------------------------------------------------------------------------- #
# 翻译规则镜像（本次事故丢失的那一项）
# --------------------------------------------------------------------------- #
def test_rules_roundtrip(work_dir: Path) -> None:
    path = write_rules("保持人名不译。\n第二行规则。", work_dir)
    assert path == work_dir / "rules.txt"
    assert read_rules(work_dir) == "保持人名不译。\n第二行规则。"


def test_rules_missing_returns_empty(work_dir: Path) -> None:
    assert read_rules(work_dir) == ""


def test_rules_strips_surrounding_whitespace(work_dir: Path) -> None:
    write_rules("\n\n  规则  \n\n", work_dir)
    assert read_rules(work_dir) == "规则"


def test_rules_written_as_utf8_sig_for_notepad(work_dir: Path) -> None:
    write_rules("规则", work_dir)
    raw = (work_dir / "rules.txt").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "Windows 记事本需要 BOM 才不乱码"


# --------------------------------------------------------------------------- #
# 免翻短语镜像
# --------------------------------------------------------------------------- #
def test_exempt_roundtrip_preserves_order_and_dedupes(work_dir: Path) -> None:
    write_exempt(["OK", "Yes", "OK", "  Hmm  "], work_dir)
    assert read_exempt(work_dir) == ["OK", "Yes", "Hmm"]


def test_exempt_missing_returns_empty(work_dir: Path) -> None:
    assert read_exempt(work_dir) == []


def test_exempt_ignores_blank_and_comment_lines(work_dir: Path) -> None:
    (work_dir / "exempt.txt").write_text(
        "OK\n\n# 这是注释\nYes\n   \n", encoding="utf-8"
    )
    assert read_exempt(work_dir) == ["OK", "Yes"]


# --------------------------------------------------------------------------- #
# 保存：三项资产全部落盘
# --------------------------------------------------------------------------- #
def test_save_profile_writes_all_mirrors(work_dir: Path) -> None:
    save_profile(_full_profile(), work_dir)
    assert (work_dir / "profile.json").exists()
    assert (work_dir / "glossary.csv").exists()
    assert (work_dir / "rules.txt").exists()
    assert (work_dir / "exempt.txt").exists()
    assert read_rules(work_dir) == "保持人名不译，语气口语化。"
    assert read_exempt(work_dir) == ["OK", "Yes"]


def test_save_profile_roundtrip_is_lossless(work_dir: Path) -> None:
    save_profile(_full_profile(), work_dir)
    loaded, notes = load_profile(work_dir)
    assert notes == [], f"正常往返不应产生恢复提示：{notes}"
    assert loaded.terms == {"Alice": "爱丽丝", "Bob": "鲍勃"}
    assert loaded.exempt_english == ["OK", "Yes"]
    assert loaded.prompt_overrides["system_append"] == "保持人名不译，语气口语化。"


def test_save_profile_is_atomic_and_leaves_no_tmp(work_dir: Path) -> None:
    save_profile(_full_profile(), work_dir)
    save_profile(_full_profile(), work_dir)
    assert not (work_dir / "profile.json.tmp").exists()
    assert (work_dir / "profile.json").is_file()
    # 第二次保存应留下第一版的 .bak
    assert (work_dir / "profile.json.bak").is_file()
    json.loads((work_dir / "profile.json").read_text(encoding="utf-8"))


def test_write_mirrors_returns_three_paths(work_dir: Path) -> None:
    paths = write_mirrors(_full_profile(), work_dir)
    assert len(paths) == 3
    assert all(p.exists() for p in paths)


# --------------------------------------------------------------------------- #
# 恢复：主文件丢失 / 损坏 / 字段为空
# --------------------------------------------------------------------------- #
def test_load_profile_recovers_everything_when_main_file_missing(work_dir: Path) -> None:
    """事故复现：profile.json 被删，三项资产都必须从镜像复活。"""
    save_profile(_full_profile(), work_dir)
    (work_dir / "profile.json").unlink()

    loaded, notes = load_profile(work_dir)
    assert loaded.terms == {"Alice": "爱丽丝", "Bob": "鲍勃"}
    assert loaded.exempt_english == ["OK", "Yes"]
    assert loaded.prompt_overrides["system_append"] == "保持人名不译，语气口语化。"
    assert notes, "应当给出恢复提示（用户需要知道发生了什么）"
    assert any("glossary.csv" in n for n in notes)
    assert any("rules.txt" in n for n in notes)
    assert any("exempt.txt" in n for n in notes)


def test_load_profile_recovers_rules_when_overrides_empty(work_dir: Path) -> None:
    """只丢规则的情形：profile.json 还在，但 system_append 被清空。"""
    save_profile(_full_profile(), work_dir)
    bare = TermProfile(terms={"Alice": "爱丽丝"},
                       exempt_english=["OK", "Yes"], prompt_overrides={})
    bare.save(work_dir / "profile.json")

    loaded, notes = load_profile(work_dir)
    assert loaded.prompt_overrides["system_append"] == "保持人名不译，语气口语化。"
    assert any("rules.txt" in n for n in notes)


def test_load_profile_survives_corrupt_json(work_dir: Path) -> None:
    save_profile(_full_profile(), work_dir)
    (work_dir / "profile.json").write_text("{ 这不是 JSON", encoding="utf-8")

    loaded, notes = load_profile(work_dir)  # 不抛异常
    assert loaded.prompt_overrides["system_append"] == "保持人名不译，语气口语化。"
    assert loaded.exempt_english == ["OK", "Yes"]
    assert any("读取失败" in n for n in notes)


def test_empty_mirror_does_not_wipe_main_file(work_dir: Path) -> None:
    """镜像为空视为「无信息」，绝不能反过来把主文件的规则抹掉。"""
    TermProfile(prompt_overrides={"system_append": "主文件里的规则"},
                exempt_english=["OK"]).save(work_dir / "profile.json")
    (work_dir / "rules.txt").write_text("", encoding="utf-8")
    (work_dir / "exempt.txt").write_text("", encoding="utf-8")

    loaded, notes = load_profile(work_dir)
    assert loaded.prompt_overrides["system_append"] == "主文件里的规则"
    assert loaded.exempt_english == ["OK"]
    assert notes == []


def test_mirror_overrides_stale_main_file(work_dir: Path) -> None:
    """镜像比主文件新（用户手改了 rules.txt）→ 以镜像为准。"""
    TermProfile(prompt_overrides={"system_append": "旧规则"}).save(
        work_dir / "profile.json")
    write_rules("用户手改的新规则", work_dir)

    loaded, _ = load_profile(work_dir)
    assert loaded.prompt_overrides["system_append"] == "用户手改的新规则"


def test_load_profile_fresh_dir_is_silent(work_dir: Path) -> None:
    loaded, notes = load_profile(work_dir)
    assert notes == []
    assert loaded.terms == {}
    assert loaded.prompt_overrides == {}


# --------------------------------------------------------------------------- #
# 归档：换游戏目录时镜像必须一起走
# --------------------------------------------------------------------------- #
def test_archive_moves_all_profile_files(work_dir: Path) -> None:
    save_profile(_full_profile(), work_dir)
    moved = archive_profile_files(work_dir, tag="20261025-120000")

    names = sorted(p.name for p in moved)
    assert names == [
        "exempt-20261025-120000.txt",
        "glossary-20261025-120000.csv",
        "profile-20261025-120000.json",
        "rules-20261025-120000.txt",
    ]
    assert all(p.parent == work_dir / "bak" for p in moved)
    # 原地不得残留任何一份，否则新游戏会读到旧游戏的术语/规则
    for name in ("profile.json", "glossary.csv", "rules.txt", "exempt.txt"):
        assert not (work_dir / name).exists(), f"{name} 未被搬走"


def test_archive_then_load_is_empty(work_dir: Path) -> None:
    """归档后新游戏读到的必须是空 profile，不能被旧游戏污染。"""
    save_profile(_full_profile(), work_dir)
    archive_profile_files(work_dir, tag="20261025-120001")

    loaded, _ = load_profile(work_dir)
    assert loaded.terms == {}
    assert loaded.exempt_english == []
    assert loaded.prompt_overrides == {}


def test_archive_on_empty_dir_returns_empty(work_dir: Path) -> None:
    assert archive_profile_files(work_dir, tag="20261025-120002") == []
    assert not (work_dir / "bak").exists()
