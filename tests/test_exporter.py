"""Exporter 实现测试（W5 交付物，设计 §6.3 / §6.4 / §6.5）。

覆盖：
* §6.4 CSV 往返（导出 → 解析 → 字段一致）；
* §6.4 import_csv 按 uid 主键回灌 + §6.2 兜底链（不静默全量覆盖）；
* §6.3 导出熔断：未译/失败/标签不符 → 抛 ``ExportBlocked``；全部通过 → 重建 rpy；
* §6.5 bulk_accept：快照可撤（logs/）+ journal 审计；bulk_undo 还原旧状态；
  keep_declared 一并接受；add_exempt 把残片并入 exempt_english。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pytran.config import TermProfile
from pytran.exporter import (
    CSV_COLUMNS,
    CSV_DELIMITER,
    ExporterImpl,
    bulk_undo,
    project_to_csv_rows,
    rows_to_project_map,
)
from pytran.interfaces import ExportBlocked
from pytran.journal import JsonWriteJournal
from pytran.models import GameFile, Project, Segment, SegmentStatus


class FakeParser:
    """最小 regenerate 桩：输出可识别的文本即可（不校验正确性）。"""

    def regenerate(self, project: Project, file_name: str) -> str:
        lines = [f"# regenerated {file_name}"]
        for seg in project.segments:
            if seg.file == file_name:
                lines.append(f"## uid={seg.uid} target={seg.target!r}")
        return "\n".join(lines) + "\n"


def _make_project() -> Project:
    segs = [
        Segment(
            uid=1, file="a.rpy", locator="loc1", feature="f1", kind="dialogue",
            comment="", role="", source="Hello", target="你好",
            status=SegmentStatus.TRANSLATED,
        ),
        Segment(
            uid=2, file="a.rpy", locator="loc2", feature="f2", kind="dialogue",
            comment="", role="", source="World", target="世界",
            status=SegmentStatus.TRANSLATED,
        ),
        # 保留原文（keep-original）行：target == source
        Segment(
            uid=3, file="a.rpy", locator="loc3", feature="f3", kind="dialogue",
            comment="", role="", source="Press start", target="Press start",
            status=SegmentStatus.KEEP_DECLARED,
        ),
    ]
    proj = Project(
        files=[GameFile(name="a.rpy", game_relpath="game/a.rpy")],
        segments=segs,
    )
    return proj


# --------------------------------------------------------------------------- #
# §6.4 CSV 往返
# --------------------------------------------------------------------------- #
def test_export_csv_roundtrip(work_dir: Path) -> None:
    proj = _make_project()
    exp = ExporterImpl(FakeParser())
    path = exp.export_csv(proj, work_dir)
    assert path == work_dir / "output" / "translation.csv"
    assert path.exists()

    rows = list(path.read_text(encoding="utf-8-sig").splitlines())
    # 表头
    header = rows[0].split(CSV_DELIMITER)
    assert header == CSV_COLUMNS
    # 数据行按 uid 定位
    by_uid = {}
    for line in rows[1:]:
        cells = line.split(CSV_DELIMITER)
        by_uid[cells[0]] = cells
    assert by_uid["1"][5] == "你好"
    assert by_uid["3"][5] == "Press start"


def test_project_to_csv_rows_shape() -> None:
    proj = _make_project()
    rows = project_to_csv_rows(proj)
    assert rows[0] == CSV_COLUMNS
    assert len(rows) == 1 + len(proj.segments)


def test_rows_to_project_map_with_and_without_header() -> None:
    with_header = [list(CSV_COLUMNS), ["1", "loc1", "f1", "", "Hello", "你好"]]
    m1 = rows_to_project_map(with_header)
    assert m1[1]["target"] == "你好"
    # 无表头纯数据
    without = [["2", "loc2", "f2", "", "World", "世界"]]
    m2 = rows_to_project_map(without)
    assert m2[2]["target"] == "世界"
    # 脏行（列不足）被忽略
    assert 99 not in rows_to_project_map([["99", "x"]])


# --------------------------------------------------------------------------- #
# §6.4 import_csv（uid 主键 + §6.2 兜底）+ 不静默全量覆盖
# --------------------------------------------------------------------------- #
def test_import_csv_by_uid(work_dir: Path, profile: TermProfile) -> None:
    proj = _make_project()
    exp = ExporterImpl(FakeParser())
    # 手改 uid=1 的译文
    csv_path = work_dir / "in.csv"
    csv_path.write_text(
        CSV_DELIMITER.join(CSV_COLUMNS) + "\n"
        + CSV_DELIMITER.join(["1", "loc1", "f1", "", "Hello", "你好吗"]) + "\n",
        encoding="utf-8-sig",
    )
    updated, unmatched = exp.import_csv(proj, csv_path, profile)
    assert updated == 1
    assert unmatched == 0
    seg1 = next(s for s in proj.segments if s.uid == 1)
    assert seg1.target == "你好吗"
    assert seg1.status == SegmentStatus.TRANSLATED


def test_import_csv_does_not_silent_overwrite_all(work_dir: Path, profile: TermProfile) -> None:
    """CSV 只含 uid=2 的改译：uid=1/3 的译文必须保持原样。"""
    proj = _make_project()
    exp = ExporterImpl(FakeParser())
    csv_path = work_dir / "in.csv"
    csv_path.write_text(
        CSV_DELIMITER.join(CSV_COLUMNS) + "\n"
        + CSV_DELIMITER.join(["2", "loc2", "f2", "", "World", "世界改"]) + "\n",
        encoding="utf-8-sig",
    )
    exp.import_csv(proj, csv_path, profile)
    seg1 = next(s for s in proj.segments if s.uid == 1)
    seg3 = next(s for s in proj.segments if s.uid == 3)
    assert seg1.target == "你好"           # 未被覆盖
    assert seg3.target == "Press start"    # 未被覆盖
    seg2 = next(s for s in proj.segments if s.uid == 2)
    assert seg2.target == "世界改"


def test_import_csv_empty_target_back_to_pending(work_dir: Path, profile: TermProfile) -> None:
    proj = _make_project()
    exp = ExporterImpl(FakeParser())
    csv_path = work_dir / "in.csv"
    csv_path.write_text(
        CSV_DELIMITER.join(CSV_COLUMNS) + "\n"
        + CSV_DELIMITER.join(["1", "loc1", "f1", "", "Hello", ""]) + "\n",
        encoding="utf-8-sig",
    )
    exp.import_csv(proj, csv_path, profile)
    seg1 = next(s for s in proj.segments if s.uid == 1)
    assert seg1.status == SegmentStatus.PENDING


# --------------------------------------------------------------------------- #
# §6.3 导出熔断
# --------------------------------------------------------------------------- #
def test_export_rpy_when_clean(work_dir: Path) -> None:
    proj = _make_project()
    exp = ExporterImpl(FakeParser())
    written = exp.export_rpy(proj, work_dir)
    assert len(written) == 1
    assert (work_dir / "output" / "a.rpy").exists()


def test_export_rpy_blocks_on_untranslated(work_dir: Path) -> None:
    proj = _make_project()
    proj.segments[0].target = ""
    proj.segments[0].status = SegmentStatus.PENDING
    exp = ExporterImpl(FakeParser())
    with pytest.raises(ExportBlocked) as exc:
        exp.export_rpy(proj, work_dir)
    assert "untranslated" in exc.value.details


def test_export_rpy_blocks_on_failed(work_dir: Path) -> None:
    proj = _make_project()
    proj.segments[1].status = SegmentStatus.FAILED
    proj.segments[1].error = "english"
    exp = ExporterImpl(FakeParser())
    with pytest.raises(ExportBlocked) as exc:
        exp.export_rpy(proj, work_dir)
    assert "failed" in exc.value.details


def test_export_rpy_blocks_on_tag_mismatch(work_dir: Path) -> None:
    proj = _make_project()
    # 标签计数不一致：source 含 {w}，target 去掉
    proj.segments[0].source = "Wait{w=0.5}"
    proj.segments[0].target = "等待"
    proj.segments[0].status = SegmentStatus.TRANSLATED
    exp = ExporterImpl(FakeParser())
    with pytest.raises(ExportBlocked) as exc:
        exp.export_rpy(proj, work_dir)
    assert "tag_check" in exc.value.details


def test_export_rpy_force_overrides_fuse(work_dir: Path) -> None:
    proj = _make_project()
    proj.segments[0].target = ""
    proj.segments[0].status = SegmentStatus.PENDING
    exp = ExporterImpl(FakeParser())
    # §6.3 用户强制导出：忽略熔断
    written = exp.export_rpy_forced(proj, work_dir)
    assert (work_dir / "output" / "a.rpy").exists()
    assert len(written) == 1


# --------------------------------------------------------------------------- #
# §6.5 bulk_accept + 撤销 + keep_declared + exempt
# --------------------------------------------------------------------------- #
def test_bulk_accept_snapshots_and_sets_reviewed(work_dir: Path) -> None:
    proj = _make_project()
    exp = ExporterImpl(FakeParser())
    journal = JsonWriteJournal(work_dir)
    n = exp.bulk_accept(proj, [1, 3], journal, work_dir=work_dir)
    assert n == 2
    # 接受后状态
    assert proj.segments[0].status == SegmentStatus.REVIEWED
    assert proj.segments[2].status == SegmentStatus.REVIEWED  # keep_declared 一并接受
    # 快照落盘 logs/
    snaps = list((work_dir / "logs").glob("bulk_accept-*.json"))
    assert len(snaps) == 1
    # journal 审计事务已提交
    assert list((work_dir / "logs").glob("journal-*.json"))


def test_bulk_undo_restores_prior_status(work_dir: Path) -> None:
    proj = _make_project()  # uid1=TRANSLATED, uid3=KEEP_DECLARED
    exp = ExporterImpl(FakeParser())
    journal = JsonWriteJournal(work_dir)
    exp.bulk_accept(proj, [1, 3], journal, work_dir=work_dir)
    restored = bulk_undo(proj, work_dir)
    assert restored == 2
    assert proj.segments[0].status == SegmentStatus.TRANSLATED
    assert proj.segments[2].status == SegmentStatus.KEEP_DECLARED
    # 撤销后快照标记 .undone，不可重复撤销
    assert list((work_dir / "logs").glob("bulk_accept-*.json.undone"))


def test_bulk_accept_add_exempt_collects_residual(work_dir: Path) -> None:
    proj = _make_project()
    # uid3 source="Press start"，保留原文；add_exempt 应把残片并入 profile
    profile = TermProfile()
    exp = ExporterImpl(FakeParser())
    journal = JsonWriteJournal(work_dir)
    n = exp.bulk_accept(
        proj, [3], journal, work_dir=work_dir, profile=profile, add_exempt=True
    )
    assert n == 1
    assert "Press" in profile.exempt_english
    assert "start" in profile.exempt_english


def test_bulk_accept_empty_uids_is_noop(work_dir: Path) -> None:
    proj = _make_project()
    exp = ExporterImpl(FakeParser())
    journal = JsonWriteJournal(work_dir)
    assert exp.bulk_accept(proj, [], journal, work_dir=work_dir) == 0
    assert proj.segments[0].status == SegmentStatus.TRANSLATED  # 不变


def test_bulk_accept_preserves_keep_original(work_dir: Path) -> None:
    """W5g §2：批量接受时，KEEP_ORIGINAL 行不得被强制改为 REVIEWED。"""
    proj = _make_project()
    # 追加一条人工保留原文行：target 设为原文
    proj.segments.append(Segment(
        uid=4, file="a.rpy", locator="loc4", feature="f4", kind="dialogue",
        comment="", role="", source="Skip intro", target="Skip intro",
        status=SegmentStatus.KEEP_ORIGINAL,
    ))
    exp = ExporterImpl(FakeParser())
    journal = JsonWriteJournal(work_dir)
    n = exp.bulk_accept(proj, [1, 2, 3, 4], journal, work_dir=work_dir)
    assert n == 4
    assert proj.segments[0].status == SegmentStatus.REVIEWED   # TRANSLATED → REVIEWED
    assert proj.segments[2].status == SegmentStatus.REVIEWED   # KEEP_DECLARED → REVIEWED
    # KEEP_ORIGINAL 保持原样，不被接受覆盖
    assert proj.segments[3].status == SegmentStatus.KEEP_ORIGINAL
    assert proj.segments[3].target == "Skip intro"


def test_export_writes_source_for_keep_original(work_dir: Path) -> None:
    """导出写回时，KEEP_ORIGINAL 行的残片取 source（保留原文）。"""
    proj = _make_project()
    proj.segments.append(Segment(
        uid=4, file="a.rpy", locator="loc4", feature="f4", kind="dialogue",
        comment="", role="", source="Press start", target="Press start",
        status=SegmentStatus.KEEP_ORIGINAL,
    ))
    exp = ExporterImpl(FakeParser())
    written = exp.export_rpy(proj, work_dir)
    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    # 该行的 target 即原文（保留原文），写回内容应含原文
    assert "target='Press start'" in text
