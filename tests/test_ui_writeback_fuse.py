"""写回前熔断（§6.3）回归测试 —— 2026-10-25 事故。

事故链：
1. ``backfill.match_translations`` 第 2 级缺文件约束，把 dlc.rpy 的 28 条
   strings 译文按位置写进 exp-1-3somes.rpy（见
   ``test_backfill_cross_file_strings.py``）；
2. 错位后 28 条 target 里有 14 条与 source 的标签/插值不匹配，
   ``evaluate_export_fuse`` **确实检出了** 14 条 ``tag_check``；
3. 但 UI 的写回入口 ``_on_writeback`` 直接调 ``write_back_project``，
   **完全绕过熔断** → 错译文照样落盘，用户直到看见
   ``new "{image=it_qu.webp} 波泽拉的水晶球"`` 才发现。

本文件守第 3 环：写回必须先过熔断，且用户不确认就不写。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="写回熔断测试需要 PyQt6（GUI 依赖，可选）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from pytran.models import Project, Segment, SegmentStatus  # noqa: E402
from pytran.ui import PytranApp  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法创建 QApplication：{exc}")


@pytest.fixture()
def make_app(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QDialog, "exec", lambda self: 0, raising=False)
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: None, raising=False)

    def _build(segments):
        app = PytranApp(Project(segments=segments, game_root=""),
                        work_dir=tmp_path)
        app._log = lambda *a, **k: None
        app.run()
        return app

    return _build


def _clean_segments() -> list[Segment]:
    """全部已译且标签一致 → 熔断应当放行。"""
    return [
        Segment(uid=1, file="a.rpy", locator="# game/a.rpy:1",
                feature="translate chinese t:", kind="dialogue",
                comment="", role="", source="Hello there",
                target="你好啊", status=SegmentStatus.TRANSLATED),
    ]


def _tag_mismatch_segments() -> list[Segment]:
    """复刻事故末条：原文纯文本，译文带 {image=...} 插值 → tag_check 不过。"""
    return [
        Segment(uid=2887254682, file="tl/chinese2/exp-1-3somes.rpy",
                locator="# game/tl/chinese2/exp-1-3somes.rpy:15425",
                feature="translate chinese2 strings:", kind="strings",
                comment="", role="",
                source="Take me to the Sylphir Island",
                target="{image=it_qu.webp} 波泽拉的水晶球",
                status=SegmentStatus.REVIEWED),
    ]


class _SpyExporter:
    def __init__(self) -> None:
        self.calls = 0

    def write_back_project(self, project, work_dir, journal=None):
        self.calls += 1
        return []


class _FakeDeps:
    """``_on_writeback`` 只用到 ``exporter`` 与 ``journal``。"""

    def __init__(self, exporter) -> None:
        self.exporter = exporter
        self.journal = None


def _install_spy(app) -> _SpyExporter:
    spy = _SpyExporter()
    app.deps = _FakeDeps(spy)
    return spy


def test_fuse_flags_tag_mismatch(make_app) -> None:
    """前置事实：这条事故数据确实触发熔断（不是我们凭空假设）。"""
    from pytran.backfill import evaluate_export_fuse

    app = make_app(_tag_mismatch_segments())
    fuse = evaluate_export_fuse(app.project)
    assert "tag_check" in fuse
    assert any("2887254682" in x for x in fuse["tag_check"])


def test_clean_project_passes_fuse(make_app) -> None:
    from pytran.backfill import evaluate_export_fuse

    app = make_app(_clean_segments())
    assert evaluate_export_fuse(app.project) == {}


def test_writeback_blocked_when_user_declines(make_app, monkeypatch) -> None:
    """熔断不过 + 用户选「否」→ 绝不调用写回。"""
    app = make_app(_tag_mismatch_segments())
    spy = _install_spy(app)

    asked = {"n": 0}

    def _no(*a, **k):
        asked["n"] += 1
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", _no, raising=False)

    app._on_writeback()

    assert asked["n"] == 1, "熔断不过时必须弹确认框"
    assert spy.calls == 0, "用户拒绝后不得写回"


def test_writeback_proceeds_when_user_confirms(make_app, monkeypatch) -> None:
    """熔断不过但用户明确强制 → 允许写回（保留 §6.3「用户可强制导出」）。"""
    app = make_app(_tag_mismatch_segments())
    spy = _install_spy(app)

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes,
                        raising=False)

    app._on_writeback()
    assert spy.calls == 1


def test_writeback_no_prompt_when_fuse_clean(make_app, monkeypatch) -> None:
    """熔断通过时不应打扰用户（不弹框，直接写回）。"""
    app = make_app(_clean_segments())
    spy = _install_spy(app)

    def _boom(*a, **k):
        raise AssertionError("熔断通过时不应弹确认框")

    monkeypatch.setattr(QMessageBox, "question", _boom, raising=False)

    app._on_writeback()
    assert spy.calls == 1


def test_fuse_prompt_lists_categories(make_app, monkeypatch) -> None:
    """确认框文案要说明「哪几类、各多少条」，否则用户无法判断该不该强制。"""
    app = make_app(_tag_mismatch_segments())
    _install_spy(app)

    captured = {}

    def _capture(parent, title, text, *a, **k):
        captured["title"] = title
        captured["text"] = text
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", _capture, raising=False)
    app._on_writeback()

    text = captured.get("text", "")
    assert "标签" in text
    assert "条" in text
    # 必须点出「原文/译文不对应」，让用户一眼看出是错位而不是漏译
    assert "原文" in text or "错位" in text
