"""project.json 的加载/保存（W1 解析链存储层，设计 §3.2）。

**W1 仅实现加载/保存与统计归档**，不含 2.0 迁移导入（设计 §10，属 W6）。
`migration` 留空接口、标注归属，避免越界实现被误用。

设计 §3.2 要点：
* 每次状态变更即**原子写入**（写临时文件 + `os.replace`），保证中途崩溃
  不产生半截 project.json。
* `stats` 在保存前由 segments 实时重算（设计 `ProjectStats.from_segments`），
  不依赖调用方手动维护。
* `files` 的 sha256 / imported_at 由导入流程（W6/CLI）填充；W1 保存时不
  擅自改写既有 files 的这些值。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .interfaces import ProjectStore
from .models import Project, ProjectStats, Segment, SegmentStatus

logger = logging.getLogger(__name__)

PROJECT_FILE_NAME = "project.json"


class JsonProjectStore(ProjectStore):
    """基于 project.json 的 :class:`ProjectStore` 实现（设计 §3.2）。"""

    def load(self, work_dir: Path) -> Project:
        """读取 work_dir/project.json；缺失或为空则返回一个全新空项目。"""
        path = Path(work_dir) / PROJECT_FILE_NAME
        if not path.exists():
            logger.info("project.json 不存在（%s），返回空项目", path)
            return Project()
        raw = path.read_bytes()
        if not raw.strip():
            logger.warning("project.json 为空，返回空项目（%s）", path)
            return Project()
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            from .interfaces import ProjectError

            raise ProjectError(f"project.json 损坏：{exc}") from exc
        project = Project.from_dict(data)
        # DECISION(stable-uid)：旧工程 uid 是「单次导入内 1..N 顺序号」，
        # 与新的「路径 hash + 段序号」稳定 uid 不兼容。加载时检测并迁移：
        # 按 (文件, 出现顺序) 重编——与下次导入同文件的段一一对齐，
        # 合并导入的 uid 对位因此继续有效（文件内容未变时）。
        _migrate_legacy_uids(project)
        # 防御：stats 与 segments 不一致时以 segments 重算为准。
        expected = ProjectStats.from_segments(project.segments)
        if project.stats != expected:
            logger.info("stats 与 segments 不一致，按 segments 重算")
            project.stats = expected
        return project

    def save(self, project: Project, work_dir: Path) -> None:
        """原子写入 project.json（临时文件 + os.replace，设计 §3.2）。"""
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        path = work_dir / PROJECT_FILE_NAME
        # 保存前重算 stats，避免半更新状态持久化。
        project.stats = ProjectStats.from_segments(project.segments)
        payload = project.to_dict()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        logger.debug("已保存 project.json 至 %s", path)


def _migrate_legacy_uids(project: "Project") -> None:
    """把旧工程的「1..N 顺序 uid」迁移为「路径 hash + 段序号」稳定 uid。

    判定：任一 uid < (1<<16) 且 uid 序列在单文件内连续 → 视为旧格式
    （新格式 uid 高位是路径 hash，恒 ≥ 1<<16）。迁移按 (文件, 出现顺序)
    重编，段间相对顺序不变，与下次导入的对齐结果一致。
    """
    from pytran.uids import assign_uids_flat
    segs = project.segments
    if not segs:
        return
    legacy = all(s.uid < (1 << 16) for s in segs)
    if not legacy:
        return
    logger.info(
        "检测到旧格式 uid（顺序号），迁移为稳定 uid（路径 hash + 段序号）："
        "%d 段", len(segs)
    )
    assign_uids_flat(segs)


def build_project(
    segments: list[Segment],
    files: list = None,
    game_root: str = "",
    term_profile: str = "profile.json",
    *,
    reassign_uids: bool = True,
) -> Project:
    """由解析结果组装一个可保存的空 Project（W1 导入辅助，设计 §3.2）。

    `files` 为 :class:`GameFile` 列表（来自 parser 调用方，W6 导入时补全
    sha256 / imported_at）。W1 仅负责把 segments 归档为 next_uid 递增状态。

    BUGFIX(dup-uid)：uid 全局唯一化。解析器按文件局部计数（每个
    ``parse_file`` 从 1 起），多文件导入时 uid 大面积撞车（用户实测
    2026-10-21：10 文件 1468 段仅 436 个唯一 uid）。uid 是引擎 seg_map
    / 预览回查 / 审校游标的全局主键，撞车导致跨文件互相覆盖——预览
    显示别的句子的译文、写回错行。

    DECISION(stable-uid, 用户裁决 2026-10-24)：uid 分配改为
    **「文件路径 hash + 文件内序号」**（pytran.uids.make_uid）——
    * 同文件同一条段在任何次导入得到同一 uid（文件内容不变时），
      合并导入时 uid 直接对齐，无需模糊匹配；
    * 不同文件路径 hash 不同，永不撞车；
    * 修复「每次导入从 1 重编 → 合并导入新旧 uid 撞车 → 写回后译文
      张冠李戴」事故（2026-10-24，15 文件全污染，见
      docs/BUGFIX-merge-uid-collision.md）。
    ``reassign_uids=False`` 时保留调用方给定的 uid（CSV 回填等
    同会话场景，uid 未变、无需重编）。
    """
    if reassign_uids:
        from pytran.uids import assign_uids_flat
        assign_uids_flat(segments)
    next_uid = (max((s.uid for s in segments), default=0)) + 1
    project = Project(
        version=3,
        app="pytran",
        files=list(files) if files else [],
        next_uid=next_uid,
        segments=segments,
        term_profile=term_profile,
        game_root=game_root,
    )
    project.stats = ProjectStats.from_segments(project.segments)
    return project


__all__ = ["JsonProjectStore", "PROJECT_FILE_NAME", "build_project"]
