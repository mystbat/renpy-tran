"""事务化写回日志（WriteJournal 实现，§15.2）。

根据 `interfaces.WriteJournal` 契约实现 `JsonWriteJournal`：begin/record/commit/
rollback。每笔变更记录「源、目标、备份路径」，失败时可按 journal 逆向回滚，保证
写回游戏目录是单个可整体还原的事务（设计 §15.2：先备份、记 journal、任一失败即
停止并回滚）。

API Key / 配置内容永不进入 journal 或日志（§11/§12 脱敏）。
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .interfaces import WriteJournal

JOURNAL_SUBDIR = "logs"  # journal.json 存放于 <root>/logs/


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class JsonWriteJournal:
    """基于 JSON 文件的 :class:`WriteJournal` 实现。

    构造函数接收一个工作目录 ``root``，journal 文件写到 ``<root>/logs/``；
    ``rollback`` 依据每条记录把目标恢复到变更前状态（``backup`` 存在则还原，
    否则删除新建的文件）。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._journal_dir = self._root / JOURNAL_SUBDIR
        self._active: dict[str, dict[str, Any]] = {}

    # -- public API ---------------------------------------------------------

    def begin(self, label: str) -> str:
        journal_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self._active[journal_id] = {
            "id": journal_id,
            "label": label,
            "started_at": _now_iso(),
            "status": "open",
            "entries": [],
        }
        return journal_id

    def record(
        self,
        journal_id: str,
        action: str,
        src: Path,
        dst: Path,
        backup: Optional[Path] = None,
    ) -> None:
        tx = self._require_open(journal_id)
        tx["entries"].append(
            {
                "action": action,
                "src": str(Path(src)),
                "dst": str(Path(dst)),
                "backup": None if backup is None else str(Path(backup)),
            }
        )

    def commit(self, journal_id: str) -> None:
        tx = self._require_open(journal_id)
        tx["status"] = "committed"
        tx["finished_at"] = _now_iso()
        self._persist(tx)
        self._active.pop(journal_id, None)

    def rollback(self, journal_id: str) -> None:
        tx = self._active.get(journal_id)
        if tx is None:
            # 已提交或无此事务：尝试从磁盘加载以便事后回滚
            tx = self._load(journal_id)
            if tx is None:
                raise KeyError(f"unknown journal: {journal_id}")
        tx["status"] = "rolled_back"
        tx["finished_at"] = _now_iso()
        # 逆序还原：最后写入的先撤销
        for entry in reversed(tx["entries"]):
            self._reverse(entry)
        self._persist(tx)
        self._active.pop(journal_id, None)

    # -- internals ----------------------------------------------------------

    def _require_open(self, journal_id: str) -> dict[str, Any]:
        tx = self._active.get(journal_id)
        if tx is None or tx.get("status") != "open":
            raise KeyError(f"no open journal with id {journal_id!r}")
        return tx

    def _reverse(self, entry: dict[str, Any]) -> None:
        dst = Path(entry["dst"])
        backup = entry["backup"]
        if backup is not None and Path(backup).exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(backup), dst)
        else:
            # 原目标不存在（新建）→ 删除新建产物，恢复「无此文件」状态
            if dst.exists():
                try:
                    dst.unlink()
                except IsADirectoryError:  # pragma: no cover - defensive
                    shutil.rmtree(dst, ignore_errors=True)

    def _persist(self, tx: dict[str, Any]) -> None:
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        path = self._journal_dir / f"journal-{tx['id']}.json"
        path.write_text(
            json.dumps(tx, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load(self, journal_id: str) -> Optional[dict[str, Any]]:
        path = self._journal_dir / f"journal-{journal_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):  # pragma: no cover - defensive
            return None


__all__ = ["JsonWriteJournal", "JOURNAL_SUBDIR"]
