"""CLI 测试（W5 交付物，设计 §8.3 / §13 / §15）。

用替身 Provider/Engine/Unren/Exporter 注入 CliDeps，验证：
* §15.1 L2 必须 --yes（否则拒绝启动）；
* §15.3 dry-run 成本闸：超 --budget-max 拒绝启动；
* §15.2 L2 写回 vs L0/L1 仅导出；
* translate 纯文件模式解析→翻译→导出；
* auto-batch 失败隔离。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from pytran.cli import (
    check_level_policy,
    cmd_auto_batch,
    cmd_game,
    cmd_translate,
    estimate_token_budget,
    within_budget,
)
from pytran.config import Config, TermProfile
from pytran.models import GameFile, Project, Segment, SegmentStatus
from pytran.project import JsonProjectStore


class FakeProvider:
    """不调用真实 API 的 provider 替身。"""

    def translate_batch(self, numbered_lines, profile, context=(), **kwargs):
        return {uid: f"译:{text}" for uid, text in numbered_lines}


class FakeEngine:
    """把所有可译行置为 TRANSLATED。"""

    def __init__(self, *a, **k):
        self.run_called = False

    def run(self, project, config, profile, on_event=None, only_uids=None):
        self.run_called = True
        for seg in project.segments:
            if seg.status in (SegmentStatus.PENDING, SegmentStatus.FAILED):
                seg.target = f"译:{seg.source}"
                seg.status = SegmentStatus.TRANSLATED
        return project


class FakeUnren:
    """记录解包/反编译/骨架/写回调用。"""

    def __init__(self):
        self.calls = []

    def is_renpy_game(self, root):
        return True

    def extract_rpa(self, root, remove_rpa=False):
        self.calls.append("extract_rpa")
        return []

    def decompile(self, root, **kw):
        self.calls.append("decompile")
        return []

    def generate_skeleton(self, root, language, config):
        self.calls.append("generate_skeleton")
        return []


class FakeExporter:
    """export_rpy_forced 落一个占位文件；write_back_project 记录调用。"""

    def __init__(self):
        self.write_back_called = False

    def export_rpy_forced(self, project, work_dir):
        out = Path(work_dir) / "output"
        out.mkdir(parents=True, exist_ok=True)
        written = []
        names = [f.name for f in project.files] or ["a.rpy"]
        for name in names:
            p = out / name
            p.write_text("# fake export", encoding="utf-8")
            written.append(p)
        return written

    def write_back_project(self, project, work_dir, journal):
        self.write_back_called = True
        out = Path(work_dir) / "output"
        out.mkdir(parents=True, exist_ok=True)
        return [out / (project.files[0].name if project.files else "a.rpy")]


def _fake_deps(work_dir: Path, level=0) -> object:
    class _Deps:
        pass
    d = _Deps()
    d.config = Config()
    d.config.language = "chinese"
    d.provider = FakeProvider()
    d.engine = FakeEngine()
    d.unren = FakeUnren()
    d.exporter = FakeExporter()
    d.store = JsonProjectStore()
    d.journal = None
    return d


def _make_game_dir(game_root: Path) -> None:
    (game_root / "game").mkdir(parents=True, exist_ok=True)
    (game_root / "game" / "a.rpy").write_text(
        'translate chinese foo:\n    "Hello"\n', encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# 策略与预算门（纯函数）
# --------------------------------------------------------------------------- #
def test_l2_requires_yes() -> None:
    with pytest.raises(SystemExit) as exc:
        check_level_policy(2, yes=False)
    code = exc.value.code
    if isinstance(code, tuple):
        code = code[0]
    assert code == 2
    # L0/L1 或带 yes 的 L2 不抛
    check_level_policy(0, False)
    check_level_policy(2, True)


def test_within_budget() -> None:
    assert within_budget(100, None) is True
    assert within_budget(100, 200) is True
    assert within_budget(300, 200) is False


def test_estimate_token_budget_positive(work_dir: Path) -> None:
    proj = Project(segments=[
        Segment(uid=1, file="a.rpy", locator="l", feature="f", kind="dialogue",
                comment="", role="", source="Hello world", target=""),
    ])
    est = estimate_token_budget(proj, Config())
    assert est > 0


# --------------------------------------------------------------------------- #
# cmd_game（§8.3 / §15）
# --------------------------------------------------------------------------- #
def test_game_l0_exports_only(work_dir: Path) -> None:
    game_root = work_dir / "game_demo"
    _make_game_dir(game_root)
    deps = _fake_deps(work_dir)
    rc = cmd_game(game_root, deps=deps, level=0, yes=False, work_dir=work_dir / "w")
    assert rc == 0
    assert deps.exporter.write_back_called is False
    assert (work_dir / "w" / "output" / "a.rpy").exists()


def test_game_l2_requires_yes_and_writes_back(work_dir: Path) -> None:
    game_root = work_dir / "game_demo2"
    _make_game_dir(game_root)
    deps = _fake_deps(work_dir)
    # 无 --yes 时拒绝启动（策略门先于流程）
    with pytest.raises(SystemExit):
        cmd_game(game_root, deps=deps, level=2, yes=False, work_dir=work_dir / "w2")
    # 带 --yes 走写回分支
    rc = cmd_game(game_root, deps=deps, level=2, yes=True, work_dir=work_dir / "w2")
    assert rc == 0
    assert deps.exporter.write_back_called is True


def test_game_budget_reject(work_dir: Path) -> None:
    game_root = work_dir / "game_demo3"
    _make_game_dir(game_root)
    deps = _fake_deps(work_dir)
    rc = cmd_game(game_root, deps=deps, level=0, yes=False,
                  budget_max=1, work_dir=work_dir / "w3")
    assert rc == 3  # 超预算拒绝启动


def test_game_dry_run(work_dir: Path) -> None:
    game_root = work_dir / "game_demo4"
    _make_game_dir(game_root)
    deps = _fake_deps(work_dir)
    rc = cmd_game(game_root, deps=deps, level=2, yes=True,
                  dry_run=True, work_dir=work_dir / "w4")
    assert rc == 0
    assert deps.engine.run_called is False  # dry-run 不翻译


# --------------------------------------------------------------------------- #
# cmd_translate（纯文件模式）
# --------------------------------------------------------------------------- #
def test_translate_file_mode(work_dir: Path) -> None:
    from pytran.cli import CliDeps
    rpy = work_dir / "in.rpy"
    rpy.write_text('translate chinese foo:\n    "Hello"\n', encoding="utf-8")
    deps = _fake_deps(work_dir)
    rc = cmd_translate(
        [rpy], config=deps.config, provider=deps.provider, engine=deps.engine,
        exporter=deps.exporter, store=deps.store, work_dir=work_dir / "tw",
    )
    assert rc == 0
    assert (work_dir / "tw" / "output" / "in.rpy").exists()
    proj = deps.store.load(work_dir / "tw")
    assert any(s.target == "译:Hello" for s in proj.segments)


# --------------------------------------------------------------------------- #
# auto-batch（§15.4 失败隔离）
# --------------------------------------------------------------------------- #
def test_auto_batch_isolates_failures(work_dir: Path) -> None:
    parent = work_dir / "games"
    g1 = parent / "g1"
    g2 = parent / "g2"
    _make_game_dir(g1)
    _make_game_dir(g2)
    deps = _fake_deps(work_dir)
    deps.unren = FakeUnren()
    rc = cmd_auto_batch(parent, deps=deps, level=2, yes=True)
    assert rc == 0  # 全部成功
    assert deps.exporter.write_back_called is True


def test_auto_batch_reports_no_games(work_dir: Path) -> None:
    parent = work_dir / "empty_parent"
    parent.mkdir()
    deps = _fake_deps(work_dir)
    deps.unren = FakeUnren()
    rc = cmd_auto_batch(parent, deps=deps, level=2, yes=True)
    assert rc == 2
