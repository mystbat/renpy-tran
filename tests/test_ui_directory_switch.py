"""切换游戏目录 / 术语表复活 的数据安全回归（2026-10-25 事故）。

事故经过：真实应用的 ``work_dir`` 就是仓库根（``运行.bat`` 传
``--work-dir "%~dp0."``）。旧 ``_choose_directory`` 在「已有段」或「目录变了」
时**直接 unlink** ``profile.json`` 与 ``project.json`` 且不备份——用户选一次
目录，12,099 段译文与 118 条术语表当场消失，``profile.json`` 全盘无副本。

本模块锁定修复后的契约：

* 换目录前**必须**弹确认；用户选「否」→ 工程完全不变；
* 确认后**先备份** ``project.json`` / ``profile.json`` 到 ``bak/``，再重置；
* 旧 ``profile.json`` 是**移入 bak/**，不是删除；
* 重复选同一目录 → 视为无切换，绝不触碰既有数据；
* ``profile.json`` 丢失时，``_load_profile`` 能从 ``glossary.csv`` 复活术语。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pytran.config import TermProfile
from pytran.models import Project, Segment, SegmentStatus
from pytran.project import JsonProjectStore
from pytran.ui import PytranApp


@pytest.fixture(scope="module")
def qt_app():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover - 平台不支持时跳过
        pytest.skip(f"无法导入 QtWidgets：{exc}")
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法创建 QApplication：{exc}")
    return app


def _seg(uid: int, source: str, target: str) -> Segment:
    return Segment(
        uid=uid,
        file="script.rpy",
        locator=f"# game/script.rpy:{uid + 1}",
        feature=f"translate chinese k{uid}:",
        kind="dialogue",
        comment="",
        role="",
        source=source,
        target=target,
        status=SegmentStatus.TRANSLATED,
    )


def _project(game_root: str = "C:/game_old") -> Project:
    return Project(
        version=3,
        app="pytran",
        game_root=game_root,
        segments=[_seg(1, "Hello", "你好"), _seg(2, "Goodbye", "再见")],
    )


def _prepare_workdir(tmp_path: Path) -> None:
    """落盘一份「真实工程」：project.json + profile.json（含三个镜像）。"""
    from pytran.profile_store import save_profile

    JsonProjectStore().save(_project(), tmp_path)
    save_profile(
        TermProfile(
            game="HOROS",
            terms={"Ian": "伊恩", "Aleneite": "阿勒奈特"},
            exempt_english=["Menu"],
            prompt_overrides={"system_append": "保持人名不译"},
        ),
        tmp_path,
    )


def _make_app(qt_app, monkeypatch, tmp_path: Path, game_root="C:/game_old") -> PytranApp:
    from PyQt6.QtWidgets import QApplication

    monkeypatch.setattr(QApplication, "exec", lambda self: 0, raising=False)
    app = PytranApp(_project(game_root), work_dir=tmp_path)
    app._log = lambda *a, **k: None  # type: ignore[assignment]
    app.run()
    return app


def _pick_directory(monkeypatch, target: str) -> None:
    from PyQt6.QtWidgets import QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: target), raising=False,
    )


def _answer(monkeypatch, yes: bool) -> None:
    from PyQt6.QtWidgets import QMessageBox

    answer = (QMessageBox.StandardButton.Yes if yes
              else QMessageBox.StandardButton.No)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: answer), raising=False,
    )


# ---------------------------------------------------------------------------
# 1. 用户取消 → 工程完全不变
# ---------------------------------------------------------------------------
def test_cancel_keeps_project_and_profile(qt_app, monkeypatch, tmp_path) -> None:
    _prepare_workdir(tmp_path)
    app = _make_app(qt_app, monkeypatch, tmp_path)
    try:
        before_segments = len(app.project.segments)
        _pick_directory(monkeypatch, "C:/game_new")
        _answer(monkeypatch, yes=False)

        app._choose_directory()

        assert len(app.project.segments) == before_segments
        assert app.project.game_root == "C:/game_old"
        assert (tmp_path / "profile.json").exists()
        assert not (tmp_path / "bak").exists()
        saved = JsonProjectStore().load(tmp_path)
        assert len(saved.segments) == before_segments
    finally:
        app._window.close()


# ---------------------------------------------------------------------------
# 2. 用户确认 → 先备份再清空，旧术语表移入 bak/（不是删除）
# ---------------------------------------------------------------------------
def test_confirm_backs_up_before_clearing(qt_app, monkeypatch, tmp_path) -> None:
    _prepare_workdir(tmp_path)
    app = _make_app(qt_app, monkeypatch, tmp_path)
    try:
        _pick_directory(monkeypatch, "C:/game_new")
        _answer(monkeypatch, yes=True)

        app._choose_directory()

        # 工程已切到新目录、段已清空
        assert app.project.game_root == "C:/game_new"
        assert app.project.segments == []

        bak = tmp_path / "bak"
        assert bak.is_dir()
        assert list(bak.glob("project-*.json")), "project.json 未备份"
        moved = list(bak.glob("profile-switched-*.json"))
        assert moved, "旧 profile.json 应移入 bak/ 而非删除"

        # 备份里必须还留着 118 条级别的术语与翻译规则
        snap = TermProfile.load(moved[0])
        assert snap.terms.get("Ian") == "伊恩"
        assert snap.prompt_overrides.get("system_append") == "保持人名不译"

        # BUGFIX(profile-persist)：镜像也必须一起搬走，否则新游戏会读到旧游戏的
        # 术语/规则（load_profile 会把残留的 glossary.csv 合并进来）
        assert list(bak.glob("glossary-switched-*.csv")), "glossary.csv 未移入 bak/"
        assert list(bak.glob("rules-switched-*.txt")), "rules.txt 未移入 bak/"
        assert list(bak.glob("exempt-switched-*.txt")), "exempt.txt 未移入 bak/"
        for name in ("glossary.csv", "rules.txt", "exempt.txt"):
            assert not (tmp_path / name).exists(), f"{name} 应已移走，否则污染新游戏"

        # 当前 profile.json 已不在原位（不污染新游戏）
        assert not (tmp_path / "profile.json").exists()
        # 磁盘上的 project.json 也已是新工程
        assert JsonProjectStore().load(tmp_path).game_root == "C:/game_new"
    finally:
        app._window.close()


# ---------------------------------------------------------------------------
# 2b. 换目录后重新加载 profile → 必须是空的（旧游戏资产不得复活）
# ---------------------------------------------------------------------------
def test_switch_leaves_no_old_game_assets(qt_app, monkeypatch, tmp_path) -> None:
    _prepare_workdir(tmp_path)
    app = _make_app(qt_app, monkeypatch, tmp_path)
    try:
        _pick_directory(monkeypatch, "C:/game_new")
        _answer(monkeypatch, yes=True)
        app._choose_directory()

        reloaded = app._load_profile()
        assert reloaded.terms == {}, "新游戏不应继承旧游戏的术语表"
        assert reloaded.exempt_english == []
        assert reloaded.prompt_overrides.get("system_append") in (None, "")
    finally:
        app._window.close()


# ---------------------------------------------------------------------------
# 3. 重选同一目录 → 无切换，数据分毫不动
# ---------------------------------------------------------------------------
def test_same_directory_does_not_touch_data(qt_app, monkeypatch, tmp_path) -> None:
    _prepare_workdir(tmp_path)
    app = _make_app(qt_app, monkeypatch, tmp_path)
    try:
        _pick_directory(monkeypatch, "C:/game_old")
        # 若实现误弹确认框，这里会让它答「否」——测试仍应通过（无清空）
        _answer(monkeypatch, yes=False)

        app._choose_directory()

        assert len(app.project.segments) == 2
        assert (tmp_path / "profile.json").exists()
        assert not (tmp_path / "bak").exists()
        assert JsonProjectStore().load(tmp_path).stats.translated == 2
    finally:
        app._window.close()


# ---------------------------------------------------------------------------
# 4. profile.json 丢失 → 从 glossary.csv 复活术语
# ---------------------------------------------------------------------------
def test_load_profile_recovers_terms_from_glossary_csv(tmp_path) -> None:
    from pytran.terms import write_glossary_csv_rows

    write_glossary_csv_rows(
        [("Ian", "伊恩"), ("Aleneite", "阿勒奈特"), ("Amazonia", "亚马逊")],
        tmp_path,
    )
    assert not (tmp_path / "profile.json").exists()

    app = PytranApp(Project(segments=[]), work_dir=tmp_path)
    app._log = lambda *a, **k: None  # type: ignore[assignment]
    profile = app._load_profile()

    assert len(profile.terms) == 3
    assert profile.terms["Ian"] == "伊恩"
    assert profile.terms["Amazonia"] == "亚马逊"


# ---------------------------------------------------------------------------
# 5. profile.json 存在但缺条目 → CSV 补齐，且以 profile 为底不丢独有条目
# ---------------------------------------------------------------------------
def test_load_profile_merges_csv_without_dropping_profile_only_terms(tmp_path) -> None:
    from pytran.terms import write_glossary_csv_rows

    TermProfile(terms={"OnlyInProfile": "仅存于profile"}).save(tmp_path / "profile.json")
    write_glossary_csv_rows([("Ian", "伊恩")], tmp_path)

    app = PytranApp(Project(segments=[]), work_dir=tmp_path)
    app._log = lambda *a, **k: None  # type: ignore[assignment]
    profile = app._load_profile()

    assert profile.terms["Ian"] == "伊恩"
    assert profile.terms["OnlyInProfile"] == "仅存于profile"


# ---------------------------------------------------------------------------
# 6. 守卫自检：conftest 必须拦住写仓库根
# ---------------------------------------------------------------------------
def test_conftest_guard_blocks_repo_root_writes() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    with pytest.raises(AssertionError):
        JsonProjectStore().save(Project(segments=[]), repo_root)
    # 仓库根的真实 project.json 未被测试改动
    real = repo_root / "project.json"
    if real.exists():
        payload = json.loads(real.read_text(encoding="utf-8-sig"))
        assert payload.get("segments") or payload.get("game_root")
