"""数据模型（契约文件，T0 冻结 —— 任何会话不得修改）。

对应设计文档 docs/3.0-重构设计文档.md §3（Segment/Project）。
字段注释均引用章节号；变更需经总控裁决。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class SegmentStatus(str, enum.Enum):
    """片段状态。设计文档 §3.1；`keep_declared` 见 §5.4。"""

    PENDING = "pending"                  # 待翻译
    TRANSLATED = "translated"            # 引擎译文已通过校验
    REVIEWED = "reviewed"                # 人工已接受（自动重译不再覆盖，§6.5）
    FAILED = "failed"                    # 最终失败（reason 见 Segment.error）
    KEEP_DECLARED = "keep_declared"      # AI 申报保留，待人工裁定（§5.4）
    KEEP_ORIGINAL = "keep_original"      # 人工保留原文：不译、不进重译队列（W5g §2.1）
    SKIPPED = "skipped"                  # 解析时无法归类，原样保留（§14 风险表）


@dataclass
class Segment:
    """一个可翻译行。字段全集 = 设计文档 §3.1 示例 JSON。"""

    uid: int
    file: str                            # 相对项目根的 rpy 文件名
    locator: str                         # 如 "# game/scripts/story/chapter_02.rpy:22"
    feature: str                         # 特征码，如 "translate chinese chapter_two_6901eeea:"；仅校验用（§3.1 要点）
    kind: str                            # "dialogue" | "strings"（§3.1）
    comment: str                         # 原文注释行（old/new 块为 old 行内容），重建用
    role: str                            # 角色名，可空
    source: str                          # 原文
    target: Optional[str] = None         # 译文
    status: SegmentStatus = SegmentStatus.PENDING
    attempts: int = 0                    # 重译轮次计数（§5.3）
    error: Optional[str] = None          # 失败原因：empty/quote/english/tags/missing/round_budget/model_fixed_point/…
    indent: int = 4                      # 还原 rpy 时的缩进空格数（§3.1）

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "file": self.file,
            "locator": self.locator,
            "feature": self.feature,
            "kind": self.kind,
            "comment": self.comment,
            "role": self.role,
            "source": self.source,
            "target": self.target,
            "status": self.status.value,
            "attempts": self.attempts,
            "error": self.error,
            "indent": self.indent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            uid=int(data["uid"]),
            file=data["file"],
            locator=data.get("locator", ""),
            feature=data.get("feature", ""),
            kind=data.get("kind", "dialogue"),
            comment=data.get("comment", ""),
            role=data.get("role", ""),
            source=data.get("source", ""),
            target=data.get("target"),
            status=SegmentStatus(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            error=data.get("error"),
            indent=int(data.get("indent", 4)),
        )


@dataclass
class GameFile:
    """project.json `files` 条目（§3.2）。"""

    name: str                            # 相对项目根
    sha256: str = ""
    imported_at: str = ""                # ISO 时间戳
    game_relpath: str = ""               # 游戏目录内相对路径（写回映射，§8.3）；纯文件模式为空


@dataclass
class ProjectStats:
    """按状态统计的行数（§3.2）。"""

    pending: int = 0
    translated: int = 0
    reviewed: int = 0
    failed: int = 0
    keep_declared: int = 0
    keep_original: int = 0
    skipped: int = 0

    @classmethod
    def from_segments(cls, segments: list[Segment]) -> "ProjectStats":
        stats = cls()
        for seg in segments:
            key = seg.status.value
            if hasattr(stats, key):
                setattr(stats, key, getattr(stats, key) + 1)
        return stats


@dataclass
class Project:
    """工作区状态（§3.2）。原子写入由 ProjectStore 负责（interfaces.py）。"""

    version: int = 3
    app: str = "pytran"
    files: list[GameFile] = field(default_factory=list)
    next_uid: int = 1
    segments: list[Segment] = field(default_factory=list)
    term_profile: str = "profile.json"
    game_root: str = ""                  # 关联游戏目录；空 = 纯文件模式（§8.3）
    stats: ProjectStats = field(default_factory=ProjectStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "app": self.app,
            "files": [
                {
                    "name": f.name,
                    "sha256": f.sha256,
                    "imported_at": f.imported_at,
                    "game_relpath": f.game_relpath,
                }
                for f in self.files
            ],
            "next_uid": self.next_uid,
            "segments": [s.to_dict() for s in self.segments],
            "term_profile": self.term_profile,
            "game_root": self.game_root,
            "stats": {
                "pending": self.stats.pending,
                "translated": self.stats.translated,
                "reviewed": self.stats.reviewed,
                "failed": self.stats.failed,
                "keep_declared": self.stats.keep_declared,
                "keep_original": self.stats.keep_original,
                "skipped": self.stats.skipped,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        stats_raw = data.get("stats", {})
        return cls(
            version=int(data.get("version", 3)),
            app=data.get("app", "pytran"),
            files=[GameFile(**f) for f in data.get("files", [])],
            next_uid=int(data.get("next_uid", 1)),
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
            term_profile=data.get("term_profile", "profile.json"),
            game_root=data.get("game_root", ""),
            stats=ProjectStats(
                pending=int(stats_raw.get("pending", 0)),
                translated=int(stats_raw.get("translated", 0)),
                reviewed=int(stats_raw.get("reviewed", 0)),
                failed=int(stats_raw.get("failed", 0)),
                keep_declared=int(stats_raw.get("keep_declared", 0)),
                keep_original=int(stats_raw.get("keep_original", 0)),
                skipped=int(stats_raw.get("skipped", 0)),
            ),
        )
