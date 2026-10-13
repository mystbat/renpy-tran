"""W6 2.0 项目迁移导入测试（设计文档 §10）。

基于真实 2.0 数据 fixture（``archive/package/2.0``，gitignored）。

数据可能不在当前工作树：worktree 不携带 gitignored 目录，因此测试沿
``__file__`` 向上回溯到仓库根，复用主树里的真实 2.0 数据。若仍找不到则
整体跳过，保证用例可移植（CI / 干净克隆上不报红）。

对齐策略（被测对象）：**块序 + 行序**对齐回填，绝不以特征码为主键；
``strings`` 块因 feature 为常量 ``translate chinese strings:``，改为按原文
文本匹配。未匹配的可译行必须置 ``FAILED`` 且带 ``error`` 原因。
"""

from __future__ import annotations

import pytest
from pathlib import Path

from pytran.interfaces import Migrator
from pytran.migrate import ProjectMigrator
from pytran.models import SegmentStatus


def _resolve_archive() -> Path | None:
    """向上回溯定位 ``archive/package/2.0``（worktree 不携带 gitignored 数据）。"""
    here = Path(__file__).resolve()
    for cand in (here, *here.parents):
        p = cand / "archive" / "package" / "2.0"
        if p.is_dir():
            return p
    return None


ARCHIVE_2_0 = _resolve_archive()
SKIP = ARCHIVE_2_0 is None
SKIP_REASON = "缺少真实 2.0 fixture：archive/package/2.0（gitignored，未随 worktree 检出）"

# 真实数据实测基准（W6 迁移冒烟）：chapter_02 迁移后 TRANSLATED 行数。
CHAPTER_02_TRANSLATED_BASELINE = 4980


@pytest.fixture(scope="module")
def migrated_project(tmp_path_factory):
    """模块级：真实迁移一次，所有用例复用（避免重复读 81MB 数据）。"""
    if SKIP:
        pytest.skip(SKIP_REASON)
    work = tmp_path_factory.mktemp("migrate")
    return ProjectMigrator().migrate(ARCHIVE_2_0, work)


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
def test_migrator_conforms_to_protocol():
    """实现满足 interfaces.Migrator 契约（runtime_checkable）。"""
    assert isinstance(ProjectMigrator(), Migrator)


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
def test_migrate_persists_project_json(migrated_project, tmp_path_factory):
    """迁移产出 3.0 工程且落盘 project.json（§10 工程化产物）。"""
    work = tmp_path_factory.mktemp("persist")
    project = ProjectMigrator().migrate(ARCHIVE_2_0, work)
    assert (work / "project.json").is_file(), "JsonProjectStore 未写出 project.json"
    # 产物与 module 级迁移规模一致（回归锚点）。
    assert len(project.segments) == len(migrated_project.segments)


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
def test_chapter_02_translated_at_baseline(migrated_project):
    """chapter_02 迁移后 TRANSLATED 行数达到历史基准（真实数据实测 4980）。"""
    segs = [
        s for s in migrated_project.segments if s.file.endswith("chapter_02.rpy")
    ]
    translated = sum(1 for s in segs if s.status == SegmentStatus.TRANSLATED)
    assert (
        translated >= CHAPTER_02_TRANSLATED_BASELINE
    ), f"chapter_02 TRANSLATED={translated} < 基准 {CHAPTER_02_TRANSLATED_BASELINE}"


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
def test_failed_segments_carry_reason(migrated_project):
    """任何 FAILED 段都必须带非空 error（未匹配行需可人工核查，§10）。"""
    failed = [
        s for s in migrated_project.segments if s.status == SegmentStatus.FAILED
    ]
    for s in failed:
        assert s.error and s.error.strip(), f"uid={s.uid} 为 FAILED 但无 error 原因"


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
def test_no_unexpected_failed_on_real_data(migrated_project):
    """真实 2.0 数据应当能完整对齐：不应出现无谓的 FAILED（冒烟锚点）。"""
    failed = sum(
        1 for s in migrated_project.segments if s.status == SegmentStatus.FAILED
    )
    assert failed == 0, f"真实数据出现 {failed} 个 FAILED（应为 0）"


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
def test_total_segment_scale(migrated_project):
    """基准规模 sanity：真实数据全部文件合计约 2.6 万段。"""
    assert len(migrated_project.segments) > 20000
