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


def build_project(
    segments: list[Segment],
    files: list = None,
    game_root: str = "",
    term_profile: str = "profile.json",
) -> Project:
    """由解析结果组装一个可保存的空 Project（W1 导入辅助，设计 §3.2）。

    `files` 为 :class:`GameFile` 列表（来自 parser 调用方，W6 导入时补全
    sha256 / imported_at）。W1 仅负责把 segments 归档为 next_uid 递增状态。

    BUGFIX(dup-uid)：uid 全局唯一化。解析器按文件局部计数（每个
    ``parse_file`` 从 1 起），多文件导入时 uid 大面积撞车（用户实测
    2026-10-21：10 文件 1468 段仅 436 个唯一 uid）。uid 是引擎 seg_map
    / 预览回查 / 审校游标的全局主键，撞车导致跨文件互相覆盖——预览
    显示别的句子的译文、写回错行。此处按传入顺序统一重编 1..N。
    """
    for new_uid, seg in enumerate(segments, start=1):
        seg.uid = new_uid
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
