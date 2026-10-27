"""导入数据安全回归测试（W5f 交付物，任务书 §4）。

问题背景（任务书 §1）：原 ``_do_import_files`` 直接 ``build_project()`` 替换内存工程并覆写
``project.json``。若某次翻译已完成却**尚未写回**就重新导入，全部译文会被**静默丢弃**。

本模块锁定修复后的行为（任务书 §2/§3/§4）：

* 重新导入前先检测「已译内容」；
* 无译文 → 保持原行为（直接导入，不弹窗）；
* 有译文 → 三选项对话框（合并导入 / 清空重导 / 取消），其中「取消」及点 X / Esc 一律 noop。
* 合并导入复用 ``backfill.match_translations`` 兜底链，沿用旧 ``term_profile``（区别于清空工程）。

对话框被抽象为 ``_confirm_import_mode() -> str``，本测试**整体 monkeypatch 掉它**，因此
**不需要 PyQt6 / 显示环境**：可在纯文件/CI 环境直接跑（仓库 GUI 测试约定见 ``test_ui_*.py``）。

覆盖任务书 §4 的 8 个用例 + 备份快照相关补充用例。
"""

from __future__ import annotations

import json
from pathlib import Path

from pytran.models import Project, Segment, SegmentStatus
from pytran.project import JsonProjectStore
from pytran.ui import PytranApp


def _source_for(feature: str) -> str:
    """按 feature 推导该块的原文，供 ``_seg`` 与 ``_write_rpy`` 共用。

    DECISION(fixture-source-consistency, 2026-10-25)：合并导入的匹配依据是
    「uid + 原文」，所以测试夹具里**旧工程段的原文必须与新解析出的原文
    一致**，否则模拟的就不是「同一段被重新导入」而是「这一段被改写成了
    另一句话」——那本来就该判不匹配。早先 ``_seg`` 用 ``source=f"src {uid}"``
    而 ``_write_rpy`` 写的是固定一句话，两者矛盾，用例靠旧的「同 feature
    即继承」宽松语义才通过。
    """
    return f"source line for {feature}"


def _seg(uid: int, feature: str, *, status: SegmentStatus = SegmentStatus.PENDING,
         target: str = "") -> Segment:
    return Segment(
        uid=uid,
        file="script.rpy",
        locator=f"# game/script.rpy:{uid + 10}",
        feature=feature,
        kind="dialogue",
        comment="",
        role="",
        source=_source_for(feature),
        target=target or None,
        status=status,
    )


def _write_rpy(path: Path, features: list[str]) -> None:
    """写出一份可被 RenpyParser 解析的 .rpy（每个 feature 一个 dialogue 块）。

    原文由 ``_source_for`` 生成，与 ``_seg`` 保持一致。
    """
    lines: list[str] = []
    for f in features:
        lines.append(f)
        lines.append(f'    "{_source_for(f)}"')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _capture_logs(app: PytranApp) -> list[str]:
    """把实例 _log 替换为记录器，返回日志缓冲。"""
    buf: list[str] = []
    app._log = lambda message, level="info": buf.append(message)  # type: ignore[assignment]
    return buf


def _old_project(translated: list[Segment], *, term_profile: str = "profile.json",
                game_root: str = "") -> Project:
    return Project(game_root=game_root, term_profile=term_profile, segments=translated)


# ---------------------------------------------------------------------------
# §4 用例 1：无译文 → 直接导入，不弹窗
# ---------------------------------------------------------------------------
def test_no_translation_imports_directly_without_dialog(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:", "translate chinese k2:"])

    app = PytranApp(Project(segments=[]), work_dir=tmp_path)  # 空工程：无译文
    _capture_logs(app)

    dialog_called = []
    app._confirm_import_mode = lambda: dialog_called.append(True) or "merge"  # type: ignore[assignment]

    app._do_import_files([rpy])

    assert not dialog_called, "无译文时不应弹出任何对话框"
    assert len(app.project.segments) == 2
    assert all(s.status == SegmentStatus.PENDING for s in app.project.segments)
    assert (tmp_path / "project.json").exists()  # 直接落盘，行为不变


# ---------------------------------------------------------------------------
# §4 用例 2：合并导入，相同 feature 的已译行被继承并置 REVIEWED
# ---------------------------------------------------------------------------
def test_merge_same_feature_inherits_target_as_reviewed(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:", "translate chinese k2:"])

    old = _old_project([
        _seg(1, "translate chinese k1:", status=SegmentStatus.REVIEWED, target="译A"),
        _seg(2, "translate chinese k2:", status=SegmentStatus.TRANSLATED, target="译B"),
    ])
    app = PytranApp(old, work_dir=tmp_path)
    _capture_logs(app)
    app._confirm_import_mode = lambda: "merge"  # type: ignore[assignment]

    app._do_import_files([rpy])

    by_feat = {s.feature: s for s in app.project.segments}
    assert by_feat["translate chinese k1:"].target == "译A"
    assert by_feat["translate chinese k1:"].status == SegmentStatus.REVIEWED
    assert by_feat["translate chinese k2:"].target == "译B"
    assert by_feat["translate chinese k2:"].status == SegmentStatus.REVIEWED


# ---------------------------------------------------------------------------
# §4 用例 3：合并导入，新出现的行保持 PENDING（不污染旧译文）
# ---------------------------------------------------------------------------
def test_merge_new_row_stays_pending(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:", "translate chinese k_new:"])

    old = _old_project([
        _seg(1, "translate chinese k1:", status=SegmentStatus.TRANSLATED, target="译A"),
    ])
    app = PytranApp(old, work_dir=tmp_path)
    _capture_logs(app)
    app._confirm_import_mode = lambda: "merge"  # type: ignore[assignment]

    app._do_import_files([rpy])

    by_feat = {s.feature: s for s in app.project.segments}
    assert by_feat["translate chinese k1:"].status == SegmentStatus.REVIEWED
    new = by_feat["translate chinese k_new:"]
    assert new.status == SegmentStatus.PENDING
    assert new.target is None


# ---------------------------------------------------------------------------
# §4 用例 4：合并导入，旧译文未能匹配新结构 → 记入「遗留译文」日志
# ---------------------------------------------------------------------------
def test_merge_unmatched_old_goes_to_legacy_log(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:"])  # 新文件没有 gone 这个 key

    old = _old_project([
        _seg(1, "translate chinese k1:", status=SegmentStatus.TRANSLATED, target="译A"),
        _seg(2, "translate chinese gone:", status=SegmentStatus.REVIEWED, target="应被遗留"),
    ])
    app = PytranApp(old, work_dir=tmp_path)
    logs = _capture_logs(app)
    app._confirm_import_mode = lambda: "merge"  # type: ignore[assignment]

    app._do_import_files([rpy])

    legacy = [m for m in logs if "遗留译文" in m]
    assert legacy, "未能匹配的旧译文应写入「遗留译文」日志"
    assert any("gone" in m for m in legacy)
    # 遗留译文不污染新工程
    assert all(s.feature != "translate chinese gone:" for s in app.project.segments)


# ---------------------------------------------------------------------------
# §4 用例 5：清空重导 → project.json 备份到 bak/ 且可读回旧译文计数
# ---------------------------------------------------------------------------
def test_reset_backs_up_project_json_to_bak(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:"])

    old = _old_project([
        _seg(1, "translate chinese k1:", status=SegmentStatus.TRANSLATED, target="译A"),
        _seg(2, "translate chinese k2:", status=SegmentStatus.REVIEWED, target="译B"),
    ])
    # 先把「当前工程」落盘，备份才有内容可拷
    JsonProjectStore().save(old, tmp_path)
    app = PytranApp(old, work_dir=tmp_path)
    _capture_logs(app)
    app._confirm_import_mode = lambda: "reset"  # type: ignore[assignment]

    app._do_import_files([rpy])

    bak_files = sorted((tmp_path / "bak").glob("project-*.json"))
    assert bak_files, "清空重导应生成 bak/project-<时间戳>.json 快照"
    snap = Project.from_dict(json.loads(bak_files[0].read_text(encoding="utf-8-sig")))
    assert snap.stats.translated == 1
    assert snap.stats.reviewed == 1
    # 新工程确实被清空（全 PENDING）
    assert all(s.status == SegmentStatus.PENDING for s in app.project.segments)
    assert app.project.segments[0].target is None


# ---------------------------------------------------------------------------
# §4 用例 6：取消 → 内存工程与 project.json 均不变（幂等、安全 noop）
# ---------------------------------------------------------------------------
def test_cancel_leaves_project_and_disk_unchanged(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:"])

    old = _old_project([
        _seg(1, "translate chinese k1:", status=SegmentStatus.REVIEWED, target="译A"),
    ])
    JsonProjectStore().save(old, tmp_path)
    app = PytranApp(old, work_dir=tmp_path)
    _capture_logs(app)
    app._confirm_import_mode = lambda: "cancel"  # type: ignore[assignment]

    app._do_import_files([rpy])

    # 内存：仍是原工程对象与译文
    assert app.project is old
    assert app.project.segments[0].status == SegmentStatus.REVIEWED
    assert app.project.segments[0].target == "译A"
    # 磁盘：project.json 仍是旧译文（未被覆盖）
    reloaded = JsonProjectStore().load(tmp_path)
    assert reloaded.stats.reviewed == 1
    assert reloaded.segments[0].target == "译A"


# ---------------------------------------------------------------------------
# §4 用例 7：合并导入后 term_profile 必须保留（区别于「清空工程」）
# ---------------------------------------------------------------------------
def test_term_profile_preserved_after_merge(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:"])

    old = _old_project(
        [_seg(1, "translate chinese k1:", status=SegmentStatus.TRANSLATED, target="译A")],
        term_profile="custom_profile.json",
    )
    app = PytranApp(old, work_dir=tmp_path)
    _capture_logs(app)
    app._confirm_import_mode = lambda: "merge"  # type: ignore[assignment]

    app._do_import_files([rpy])

    assert app.project.term_profile == "custom_profile.json"
    on_disk = json.loads((tmp_path / "project.json").read_text(encoding="utf-8"))
    assert on_disk["term_profile"] == "custom_profile.json"


# ---------------------------------------------------------------------------
# §4 用例 8：_confirm_import_mode 的返回值驱动三个分支（参数化）
# ---------------------------------------------------------------------------
def test_confirm_mode_return_value_drives_branch(tmp_path: Path) -> None:
    rpy = tmp_path / "script.rpy"
    _write_rpy(rpy, ["translate chinese k1:", "translate chinese k2:"])

    cases = [
        ("merge", SegmentStatus.REVIEWED),   # 合并：继承旧译文
        ("reset", SegmentStatus.PENDING),    # 清空：全部待译
        ("cancel", SegmentStatus.REVIEWED),  # 取消：原工程不变
    ]
    for mode, expect_first_status in cases:
        old = _old_project([
            _seg(1, "translate chinese k1:", status=SegmentStatus.REVIEWED, target="译A"),
        ])
        if mode == "cancel":
            JsonProjectStore().save(old, tmp_path)
        app = PytranApp(old, work_dir=tmp_path)
        _capture_logs(app)
        app._confirm_import_mode = lambda m=mode: m  # type: ignore[assignment]

        app._do_import_files([rpy])

        if mode == "cancel":
            assert app.project is old  # 完全 noop
        else:
            by_feat = {s.feature: s for s in app.project.segments}
            assert by_feat["translate chinese k1:"].status == expect_first_status


# ---------------------------------------------------------------------------
# 补充：备份助手在无 project.json 时返回 None（无内容可备份）
# ---------------------------------------------------------------------------
def test_backup_returns_none_without_project_json(tmp_path: Path) -> None:
    app = PytranApp(Project(segments=[]), work_dir=tmp_path)
    result = app._backup_current_project()
    assert result is None
    assert not (tmp_path / "bak").exists()


# ---------------------------------------------------------------------------
# 补充：备份快照与现网工程隔离（读快照不改动当前工程）
# ---------------------------------------------------------------------------
def test_backup_snapshot_is_readable_copy(tmp_path: Path) -> None:
    old = _old_project([
        _seg(1, "translate chinese k1:", status=SegmentStatus.TRANSLATED, target="译A"),
    ])
    JsonProjectStore().save(old, tmp_path)
    app = PytranApp(old, work_dir=tmp_path)
    dst = app._backup_current_project()
    assert dst is not None and dst.exists()
    snap = Project.from_dict(json.loads(dst.read_text(encoding="utf-8-sig")))
    assert snap.stats.translated == 1
    # 备份是拷贝：删除快照不影响当前工程
    dst.unlink()
    assert JsonProjectStore().load(tmp_path).stats.translated == 1
