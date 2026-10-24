"""术语表 CSV 纯逻辑测试（W5b-续，无头，不依赖 PyQt6）。

覆盖：默认路径、写出返回路径、含逗号/引号/空/Unicode 的往返（期望排序）、
缺失文件返回空、从表格行导出、CSV 合并（同名覆盖 / 新条目追加 / 未出现保留）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pytran.config import TermProfile
from pytran.terms import (
    glossary_path,
    merge_glossary_into_profile,
    read_glossary_csv,
    write_glossary_csv,
    write_glossary_csv_rows,
)


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    return d


def test_glossary_path_default(work_dir: Path) -> None:
    assert glossary_path(work_dir) == work_dir / "glossary.csv"


def test_write_glossary_csv_returns_path(work_dir: Path) -> None:
    path = write_glossary_csv_rows([("A", "甲")], work_dir)
    assert path == work_dir / "glossary.csv"
    assert path.exists()


def test_write_read_roundtrip_with_special_chars(work_dir: Path) -> None:
    rows = [
        ("Alice, the Queen", "爱丽丝"),
        ("Bob", ""),
        (" 空原 ", ""),
        ('Cafe "X"', "咖啡"),
    ]
    write_glossary_csv_rows(rows, work_dir)
    back = read_glossary_csv(work_dir)
    # 按原文排序（Python 默认字符串序）；空原文行被忽略；
    # 译名为空保留；特殊符号未破坏。注意 空原(U+7A7A) 排在所有 ASCII 之后。
    assert back == [
        ("Alice, the Queen", "爱丽丝"),
        ("Bob", ""),
        ('Cafe "X"', "咖啡"),
        ("空原", ""),
    ]


def test_read_glossary_csv_missing_returns_empty(work_dir: Path) -> None:
    assert read_glossary_csv(work_dir) == []


def test_write_glossary_csv_rows_from_table(work_dir: Path) -> None:
    profile = TermProfile(terms={"A": "甲", "B": "乙"})
    path = write_glossary_csv(profile, work_dir)
    assert read_glossary_csv(work_dir) == [("A", "甲"), ("B", "乙")]


def test_merge_glossary_into_profile_overrides(work_dir: Path) -> None:
    write_glossary_csv_rows(
        [("A", "甲-改"), ("C", "丙新")], work_dir
    )
    profile = TermProfile(terms={"A": "甲", "B": "乙"})
    merged = merge_glossary_into_profile(profile, work_dir)
    # CSV 同名覆盖；CSV 新条目追加；profile 中未出现条目保留
    assert merged.terms["A"] == "甲-改"
    assert merged.terms["C"] == "丙新"
    assert merged.terms["B"] == "乙"


def test_merge_glossary_missing_is_noop(work_dir: Path) -> None:
    profile = TermProfile(terms={"A": "甲"})
    merged = merge_glossary_into_profile(profile, work_dir)
    assert merged.terms == {"A": "甲"}


def test_utf8_sig_bom(work_dir: Path) -> None:
    write_glossary_csv_rows([("A", "甲")], work_dir)
    raw = (work_dir / "glossary.csv").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"  # Excel 友好 BOM
