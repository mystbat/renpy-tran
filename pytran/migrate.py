"""2.0 项目迁移导入（W6）。设计文档 §10。

把 Ren'Py 2.0 的翻译产物（``rpybak/*.rpy`` + ``split_csv/bak/<base>/*-s.csv``）
迁移进 3.0 的 :class:`pytran.models.Project` 工程（project.json）。

对齐策略（§10，W6 核心）：**块序 + 行序**对齐，绝不以特征码为主键
（2.0/3.0 的 label 可能重写，特征码不可靠）。同一个 ``translate`` 块内，
3.0 解析出的 Segment 与 2.0 的 ``-s.csv`` 行按出现顺序一一对应
（FIFO，一译文一键）。``strings`` 块因为 ``old "X"`` 与 ``new "X"`` 共用
常量 feature ``translate chinese strings:``，无法用块序定位，改用**原文文本**
（source）做键匹配。

未匹配的可译行置 ``FAILED`` 并带 ``error`` 原因，供人工核查；匹配率
（TRANSLATED / (TRANSLATED+FAILED)）< 0.95 触发 §6.3 导出熔断
（``ExportBlocked``）。

本模块不依赖任何 UI / 网络；纯函数 + 数据类，便于单测。
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pytran.backfill import evaluate_export_fuse
from pytran.interfaces import ExportBlocked, Migrator
from pytran.models import GameFile, Project, Segment, SegmentStatus
from pytran.parser import RenpyParser
from pytran.project import JsonProjectStore, build_project

logger = logging.getLogger(__name__)

__all__ = ["ProjectMigrator"]

_CSV_SUFFIX_RE = re.compile(r"_(\d+)-s\.csv$")
_FENCE_SET = frozenset({"```", "~~~", "---"})
_HEADER_MARK = "定位符"
_TRANSLATE_CHINESE_PREFIX = "translate chinese "
_STRINGS_FEATURE = "translate chinese strings:"
_MATCH_RATE_THRESHOLD = 0.95


def _n_from_csv_path(csv_path: Path) -> int:
    """从 ``<base>_NN-s.csv`` 文件名提取序号，用于稳定排序。"""
    m = _CSV_SUFFIX_RE.search(csv_path.name)
    return int(m.group(1)) if m else 0


def _read_rpy_lines(path: Path) -> list[str]:
    """以 utf-8-sig 读取 rpybak，按行返回（用于区分 old/new 字符串行）。"""
    return path.read_text(encoding="utf-8-sig").splitlines()


def _iter_csv_rows(csv_path: Path):
    """逐行解析 2.0 的 ``-s.csv``，yield ``(locator, feature, target)``。

    文件可能含围栏行（``` / ~~~ / ---）、表头行（定位符|...）与空行。
    必须使用 ``csv.reader(delimiter='|')``——2.0 的 strings 行特征码是 CSV
    引用的（例如 ``old "X"`` 在文件里写作带转义引号的形式），裸
    ``split('|')`` 会破坏引用，导致 ``feat.startswith('old "')`` 永远为假。
    """
    for raw in csv_path.read_text(encoding="utf-8-sig").splitlines():
        s = raw.strip()
        if not s or s in _FENCE_SET or _HEADER_MARK in s:
            continue
        parts = next(csv.reader([raw], delimiter="|"), None)
        if not parts or len(parts) < 3:
            continue
        locator, feature, target = parts[0].strip(), parts[1].strip(), parts[2]
        if not feature:
            continue
        yield locator, feature, target


def _unquote_old_feature(feature: str) -> str:
    """从 ``old "X"`` 特征码取出内部原文 X（去 CSV 引用与两端引号）。"""
    inner = feature[4:].strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
        inner = inner[1:-1]
    return inner


def _line_no(locator: str) -> Optional[int]:
    """从 ``# game/...:N`` 定位符取 1-based 行号。"""
    m = re.search(r":(\d+)$", locator)
    return int(m.group(1)) if m else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectMigrator(Migrator):
    """按 §10 块序+行序对齐，把 2.0 译文回填进 3.0 工程。"""

    def migrate(self, old_dir: Path, work_dir: Path) -> Project:
        old_dir = Path(old_dir)
        work_dir = Path(work_dir)
        rpybak = old_dir / "rpybak"
        split_bak = old_dir / "split_csv" / "bak"

        all_segments: list[Segment] = []
        files: list[GameFile] = []

        for rpy_path in sorted(rpybak.glob("*.rpy")):
            base = rpy_path.stem
            game_rel = f"scripts/story/{base}.rpy"
            segs = RenpyParser().parse_file(rpy_path, game_relpath=game_rel)
            lines = _read_rpy_lines(rpy_path)

            dlg_rows: dict[str, list[str]] = {}
            str_rows: dict[str, str] = {}
            csv_dir = split_bak / base
            if csv_dir.is_dir():
                for csv_path in sorted(csv_dir.glob("*-s.csv"), key=_n_from_csv_path):
                    for locator, feature, target in _iter_csv_rows(csv_path):
                        if feature.startswith('old "'):
                            str_rows[_unquote_old_feature(feature)] = target
                        elif (
                            feature.startswith(_TRANSLATE_CHINESE_PREFIX)
                            and feature != _STRINGS_FEATURE
                        ):
                            dlg_rows.setdefault(feature, []).append(target)
                        else:
                            # 例如 `translate chinese strings:` 行（无 old 前缀）
                            # 或未知特征码：不参与对齐，留待人工核查。
                            logger.debug("迁移跳过非对话行 feature=%r", feature)

            self._align_dialogue(segs, dlg_rows)
            self._align_strings(segs, lines, str_rows)

            all_segments.extend(segs)
            files.append(
                GameFile(
                    name=game_rel,
                    sha256=_sha256(rpy_path),
                    imported_at=_now_iso(),
                    game_relpath=game_rel,
                )
            )

        project = build_project(
            all_segments, files=files, game_root="", term_profile="profile.json"
        )
        self._finalize(project)

        store = JsonProjectStore()
        store.save(project, work_dir)
        return project

    @staticmethod
    def _align_dialogue(
        segs: list[Segment], dlg_rows: dict[str, list[str]]
    ) -> None:
        """对话：按 feature 分组，组内按行序（FIFO）与 2.0 行一一对齐。"""
        by_feature: dict[str, list[Segment]] = {}
        for s in segs:
            if s.kind == "dialogue":
                by_feature.setdefault(s.feature, []).append(s)
        for feature, group in by_feature.items():
            rows = dlg_rows.get(feature, [])
            for i, seg in enumerate(group):
                if i < len(rows):
                    if rows[i]:
                        seg.target = rows[i]
                        seg.status = SegmentStatus.TRANSLATED
                    # 2.0 译文为空 → 保持 PENDING，交由引擎补全（非失败）
                else:
                    # 块序+行序对齐后 2.0 仍无对应行 → 真正未匹配，标记失败。
                    seg.status = SegmentStatus.FAILED
                    seg.error = "dialogue 未匹配：2.0 缺少对应译文行（行序越界）"

    @staticmethod
    def _align_strings(
        segs: list[Segment], lines: list[str], str_rows: dict[str, str]
    ) -> None:
        """字符串：``old "X"`` 是英文参考（保持 PENDING）；
        ``new "X"`` / 裸字符串 按原文 X 在 2.0 中查译文回填。"""
        for s in segs:
            if s.kind != "strings":
                continue
            ln = _line_no(s.locator)
            if ln is None or ln > len(lines):
                s.status = SegmentStatus.FAILED
                s.error = "strings 定位符无法解析"
                continue
            text = lines[ln - 1].strip()
            if text.startswith("old "):
                # 英文参考行：保留原文，不填译文（保持 PENDING）。
                continue
            # new "X" 或裸字符串：按原文文本匹配 2.0 译文。
            tgt = str_rows.get(s.source)
            if tgt:
                s.target = tgt
                s.status = SegmentStatus.TRANSLATED
            # else：2.0 无该字符串译文 → 保持 PENDING（引擎翻译）

    @staticmethod
    def _finalize(project: Project) -> None:
        """§6.3 熔断：匹配率过低则抛 ``ExportBlocked``。"""
        translated = sum(
            1 for s in project.segments if s.status == SegmentStatus.TRANSLATED
        )
        failed = sum(
            1 for s in project.segments if s.status == SegmentStatus.FAILED
        )
        total = translated + failed
        match_rate = translated / total if total else 1.0
        details = evaluate_export_fuse(project, match_rate=match_rate)
        if "match_rate" in details:
            raise ExportBlocked("迁移匹配率过低，触发 §6.3 熔断", details)
        logger.info(
            "迁移完成：TRANSLATED=%d FAILED=%d 匹配率=%.4f",
            translated,
            failed,
            match_rate,
        )
