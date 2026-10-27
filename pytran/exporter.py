"""导出层（W5 交付物之一，设计 §6.3 / §6.4 / §6.5 / §15）。

实现 ``interfaces.Exporter`` 契约（T0 冻结，仅实现不修改）：

* ``export_rpy(project, work_dir) -> list[Path]`` —— 先跑 §6.3「导出熔断」，
  未通过抛 ``ExportBlocked``；通过后调用解析器 ``regenerate`` 重建 rpy 到
  ``output/``。
* ``export_csv(project, work_dir) -> Path`` —— 按 §6.4 写出 6 列
  ``uid|定位符|特征码|角色名|原文|译文`` 的 CSV（``|`` 分隔 + csv 引号包裹，
  表头含 uid）。
* ``import_csv(project, csv_path, profile) -> (updated, unmatched)`` —— 按 uid
  主键匹配回灌，未命中走 §6.2 兜底链；逐行复跑校验（§6.5 re-validate），
  **绝不**全量静默覆盖。
* ``bulk_accept(project, uids, journal) -> int`` —— §6.5 批量接受：把受影响行
  原状态快照（logs/）并入 journal 审计事务，置 ``REVIEWED``；可撤销。

§6.3「用户可强制导出（保留未译原文）」、§6.5「写回触发」、以及「把保留原文残片
并入 ``exempt_english``」三者超出冻结契约的四个方法签名。本模块以**可选关键字
参数 / 额外方法**形式实现（不改动 interfaces.py），并在交付说明中列为
「契约变更建议」。

§9 硬约束：本模块不触碰任何 Tk / 线程，纯逻辑，可在主线程或后台线程安全调用。
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .backfill import evaluate_export_fuse
from .config import TermProfile
from .interfaces import Exporter, ExportBlocked
from .models import Project, Segment, SegmentStatus

CSV_COLUMNS = ["uid", "定位符", "特征码", "角色名", "原文", "译文"]
# DECISION(segkey-identity)：新增 key 列（段身份），CSV 往返以 key 为主键；
# uid 列保留一个版本期仅作旧文件迁移参考。
CSV_COLUMNS_V2 = ["key", "uid", "定位符", "特征码", "角色名", "原文", "译文"]
CSV_DELIMITER = "|"
CSV_FILE_NAME = "translation.csv"
BULK_SNAPSHOT_SUBDIR = "logs"


def _now_ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- #
# CSV 编解码（纯函数，便于测试）
# --------------------------------------------------------------------------- #
def project_to_csv_rows(project: Project) -> list[list[str]]:
    """把工程片段序列化为 CSV 行（含表头，v2 带 key 列）。"""
    from .identity import segment_key

    rows: list[list[str]] = [list(CSV_COLUMNS_V2)]
    for seg in project.segments:
        rows.append(
            [
                segment_key(seg.file, seg.source),
                str(seg.uid),
                seg.locator,
                seg.feature,
                seg.role,
                seg.source if seg.source is not None else "",
                seg.target if seg.target is not None else "",
            ]
        )
    return rows


class _CsvRow:
    """CSV 行 → duck-type identity.resolve_batch 所需字段。"""

    __slots__ = ("file", "locator", "feature", "source", "target")

    def __init__(self, file: str, locator: str, feature: str,
                 source: str, target: str) -> None:
        self.file = file
        self.locator = locator
        self.feature = feature
        self.source = source
        self.target = target


def rows_to_project_map(rows: list[list[str]]) -> dict[str, dict[str, str]]:
    """把 CSV 行解析成 ``{key: {列名: 值}}``。

    兼容三种格式：v2 表头（key 列）、v1 表头（uid 列）、无表头纯数据。
    v1 旧格式无 key 列，用定位符反推 file 与原文拼 key。
    """
    from .identity import segment_key
    from .backfill import locator_file

    if not rows:
        return {}
    first = rows[0]
    has_header = first and str(first[0]).strip() in ("uid", "key")
    data_rows = rows[1:] if has_header else rows
    v2 = has_header and str(first[0]).strip() == "key"
    out: dict[str, dict[str, str]] = {}
    for raw in data_rows:
        if v2:
            if len(raw) < 7:
                continue
            out[str(raw[0]).strip()] = {
                "uid": raw[1],
                "locator": raw[2],
                "feature": raw[3],
                "role": raw[4],
                "source": raw[5],
                "target": raw[6],
            }
        else:
            # v1 旧格式：按 (定位符文件, 原文) 重建 key
            if len(raw) < 6:
                continue
            f = locator_file(raw[1])
            out[segment_key(f, raw[4])] = {
                "uid": raw[0],
                "locator": raw[1],
                "feature": raw[2],
                "role": raw[3],
                "source": raw[4],
                "target": raw[5],
            }
    return out


# --------------------------------------------------------------------------- #
# Exporter 实现
# --------------------------------------------------------------------------- #
class ExporterImpl(Exporter):
    """基于 ``RenpyParser`` 的导出实现（构造注入 parser，便于测试桩替换）。"""

    def __init__(self, parser) -> None:  # noqa: ANN001 - 仅依赖 regenerate 契约
        self._parser = parser

    # -- CSV ---------------------------------------------------------------- #
    def export_csv(self, project: Project, work_dir) -> Path:  # type: ignore[override]
        work_dir = Path(work_dir)
        out_dir = work_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / CSV_FILE_NAME
        rows = project_to_csv_rows(project)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(
                f, delimiter=CSV_DELIMITER, quoting=csv.QUOTE_MINIMAL
            )
            writer.writerows(rows)
        return path

    def import_csv(
        self,
        project: Project,
        csv_path,  # type: ignore[override]
        profile: TermProfile,
    ) -> tuple[int, int]:
        """CSV 回灌（任务书 v3.1 §2.3）：按段身份 (file, source) 对位。

        DECISION(segkey-identity, 2026-10-26)：uid 是导出时的会话内快照，
        跨导入无语义（复盘缺陷 #10）。v2 CSV 携带 key 列精确对位；v1 旧
        格式按 (定位符文件, 原文) 重建 key。对不上即 unmatched——绝不猜。
        """
        from .identity import segment_key, resolve_batch, _KEY_SEP

        csv_path = Path(csv_path)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=CSV_DELIMITER)
            rows = list(reader)
        incoming = rows_to_project_map(rows)

        # 按工程段的 key 建 index；同 key 多段（重复原文桶）按 CSV 行序
        # FIFO 对位——与 identity.resolve_batch 语义一致。
        by_key: dict[str, list[Segment]] = {}
        for seg in project.segments:
            by_key.setdefault(segment_key(seg.file, seg.source), []).append(seg)

        ordered_rows: list[_CsvRow] = []
        for key, rec in incoming.items():
            parts = key.split(_KEY_SEP, 2)
            file = parts[1] if len(parts) == 3 else ""
            ordered_rows.append(_CsvRow(
                file=file,
                locator=rec["locator"], feature=rec["feature"],
                source=rec["source"], target=rec["target"],
            ))

        outcome = resolve_batch(project.segments, ordered_rows)
        updated = 0
        for m in outcome.matches:
            seg = m.segment
            seg.target = m.row.target
            _apply_imported_status(seg, profile)
            updated += 1
        unmatched = len(outcome.unmatched)
        for row in outcome.unmatched:
            logger.warning(
                "§6.4 CSV 行未匹配（key=%r source=%r target=%r），交人工处理",
                row.locator, (row.source or "")[:60], (row.target or "")[:40],
            )

        return updated, unmatched

    # -- RPY（§6.3 熔断） --------------------------------------------------- #
    def export_rpy(self, project: Project, work_dir) -> list[Path]:  # type: ignore[override]
        """§6.3 熔断：存在阻止项时抛 ``ExportBlocked``（含明细）。"""
        fuse = evaluate_export_fuse(project)
        if _fuse_blocked(fuse):
            raise ExportBlocked("导出熔断：存在未翻译/失败/标签不符/匹配率不足的行", fuse)
        return self._regenerate_all(project, work_dir)

    # -- 批量接受（§6.5） --------------------------------------------------- #
    def bulk_accept(
        self,
        project: Project,
        uids: Iterable[int],
        journal,  # type: ignore[override]  # interfaces.WriteJournal
        *,
        work_dir=None,
        profile: Optional[TermProfile] = None,
        add_exempt: bool = False,
    ) -> int:
        """§6.5 批量接受 + 快照（可撤销）。

        冻结契约签名 ``(project, uids, journal) -> int`` 完全满足；以下为 W5
        扩展（可选关键字，不影响契约调用）：
        * ``work_dir`` —— 状态快照落盘位置（默认当前目录的 logs/）；
        * ``profile`` / ``add_exempt`` —— 把被接受行的保留原文残片并入
          ``exempt_english``（§6.5 keep-original → exempt，默认关闭）。
        """
        work_dir = Path(work_dir) if work_dir else Path(".")
        by_uid = {s.uid: s for s in project.segments}
        target = [int(u) for u in uids if int(u) in by_uid]
        if not target:
            return 0

        # ① 先快照旧状态到 logs/（撤销的唯一事实来源）
        snapshot = self._snapshot_status(work_dir, by_uid, target)

        # ② 并入 journal 审计事务（§15.2 风格，标记可撤销）
        if journal is not None:
            try:
                jid = journal.begin("bulk_accept")
                journal.record(jid, "bulk_accept_snapshot", snapshot, snapshot, None)
                journal.commit(jid)
            except Exception:  # pragma: no cover - 审计失败不阻断接受
                pass

        # ③ 置 REVIEWED（已 REVIEWED 的行不变；keep_declared 一并接受）。
        # W5g §2：KEEP_ORIGINAL（人工保留原文）不在此强制接受——它保持原样，
        # 写回时取 source 残片（见 _regenerate_all / 写回残片逻辑）。
        for uid in target:
            if by_uid[uid].status == SegmentStatus.KEEP_ORIGINAL:
                continue
            by_uid[uid].status = SegmentStatus.REVIEWED

        # ④ 可选：保留原文残片并入免翻表
        if add_exempt and profile is not None:
            added = self._collect_exempt(project, target, by_uid, profile)
            merged = list(profile.exempt_english)
            for phrase in added:
                if phrase and phrase not in merged:
                    merged.append(phrase)
            profile.exempt_english = merged

        return len(target)

    # -- W5 扩展方法（不改动 interfaces.py，见交付说明「契约变更建议」） ------- #
    def export_rpy_forced(
        self,
        project: Project,
        work_dir,
        *,
        write_back: bool = False,
        journal=None,
    ) -> list[Path]:
        """§6.3 用户强制导出：忽略熔断，重建 rpy；``write_back=True`` 时触发
        §6.5/§15.2 事务化写回（保留未译原文，即 target 为空者维持 source）。"""
        written = self._regenerate_all(project, work_dir)
        if write_back:
            written = self.write_back_project(project, work_dir, journal)
        return written

    def write_back_project(
        self,
        project: Project,
        work_dir,
        journal=None,
    ) -> list[Path]:
        """§6.5/§15.2 写回触发器：委托 ``unren.UnrenImpl`` 做事务化写回。"""
        from .unren import UnrenImpl  # 局部导入避免循环依赖

        unren = UnrenImpl()
        return unren.write_back(project, Path(work_dir), journal)

    # -- 内部 --------------------------------------------------------------- #
    def _regenerate_all(self, project: Project, work_dir) -> list[Path]:
        work_dir = Path(work_dir)
        out_dir = work_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for gf in project.files:
            text = self._parser.regenerate(project, gf.name)
            out_path = out_dir / gf.name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            written.append(out_path)
        return written

    def _snapshot_status(
        self, work_dir: Path, by_uid: dict[int, Segment], uids: list[int]
    ) -> Path:
        log_dir = work_dir / BULK_SNAPSHOT_SUBDIR
        log_dir.mkdir(parents=True, exist_ok=True)
        journal_id = _now_ts() + "-" + uuid.uuid4().hex[:8]
        path = log_dir / f"bulk_accept-{journal_id}.json"
        payload = {
            "id": journal_id,
            "label": "bulk_accept",
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "prior": {str(uid): by_uid[uid].status.value for uid in uids},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _collect_exempt(
        self,
        project: Project,
        uids: list[int],
        by_uid: dict[int, Segment],
        profile: TermProfile,
    ) -> list[str]:
        """§6.5 keep-original → exempt_english：提取被接受行的英文残片。"""
        from .validators import Whitelist, compute_residual  # 局部导入避免循环

        wl = Whitelist.from_profile(profile)
        phrases: list[str] = []
        seen: set[str] = set()
        for uid in uids:
            seg = by_uid[uid]
            # keep_declared 或「保留原文」(target 与 source 一致) 的行取 source 残片
            residual, _ = compute_residual(seg.source, seg.source, wl)
            for token in residual:
                if token and token not in seen:
                    seen.add(token)
                    phrases.append(token)
        return phrases


def _fuse_blocked(fuse: dict[str, list[str]]) -> bool:
    return any(fuse.get(k) for k in ("untranslated", "failed", "tag_check", "match_rate"))


def bulk_undo(project: Project, work_dir, snapshot_path=None) -> int:
    """§6.5 撤销：按最近一次 bulk_accept 快照还原状态。

    ``snapshot_path`` 为 ``None`` 时自动选取 logs/ 下最新且未撤销的快照。
    """
    work_dir = Path(work_dir)
    if snapshot_path is None:
        snapshot_path = _latest_snapshot(work_dir)
    if snapshot_path is None or not Path(snapshot_path).exists():
        raise FileNotFoundError("未找到可撤销的 bulk_accept 快照")
    data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    prior = data.get("prior", {})
    by_uid = {seg.uid: seg for seg in project.segments}
    restored = 0
    for uid_str, status_val in prior.items():
        uid = int(uid_str)
        if uid in by_uid:
            by_uid[uid].status = SegmentStatus(status_val)
            restored += 1
    # 标记已撤销，避免重复撤销
    done = Path(snapshot_path).with_suffix(Path(snapshot_path).suffix + ".undone")
    Path(snapshot_path).rename(done)
    return restored


def _latest_snapshot(work_dir: Path) -> Optional[Path]:
    log_dir = work_dir / BULK_SNAPSHOT_SUBDIR
    if not log_dir.is_dir():
        return None
    cands = sorted(
        p for p in log_dir.glob("bulk_accept-*.json")
        if not p.name.endswith(".undone")
    )
    return cands[-1] if cands else None


def _apply_imported_status(seg: Segment, profile: TermProfile) -> None:
    """§6.5 回填后设定状态并复跑校验（空译文退回 PENDING；非空置 TRANSLATED）。

    校验仅做**提示**记录（logger.warning），绝不静默丢弃 CSV 中的人工裁定译文。
    """
    if not seg.target or not seg.target.strip():
        seg.status = SegmentStatus.PENDING
        return
    seg.status = SegmentStatus.TRANSLATED
    try:
        from .validators import Validator  # 局部导入避免循环

        detection, _repaired = Validator().check(seg.source, seg.target, profile)
        if detection in ("residual", "empty"):
            logger.warning("import_csv uid=%s 校验提示 %s（保留人工译文）", seg.uid, detection)
    except Exception:  # pragma: no cover - 校验异常不阻断导入
        logger.warning("import_csv uid=%s 校验异常（已保留译文）", seg.uid)


logger = logging.getLogger(__name__)

__all__ = [
    "ExporterImpl",
    "project_to_csv_rows",
    "rows_to_project_map",
    "bulk_undo",
    "CSV_COLUMNS",
    "CSV_DELIMITER",
]
