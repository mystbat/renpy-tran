"""BUGFIX(regenerate-uid-index)：locator 撞车时的确定性行为 + 写回前告警。"""
from pathlib import Path
from pytran.parser import RenpyParser
from pytran.project import build_project
from pytran.models import Segment, SegmentStatus, GameFile
from pytran.uids import make_uid


def _make(tmp: Path):
    f = tmp / "x.rpy"
    f.write_text(
        'translate chinese t1:\n    e "line one"\n    e "line two"\n',
        encoding="utf-8")
    p = RenpyParser()
    segs = p.parse_file(f, game_relpath="x.rpy")
    proj = build_project(segs, files=[GameFile(name="x.rpy", game_relpath="x.rpy")])
    return p, proj, f


def test_locator_collision_takes_lowest_uid(tmp_path):
    """locator 撞车时按 uid 升序取第一段（确定性，不再是谁后谁赢）。"""
    p, proj, f = _make(tmp_path)
    # 强制两段同一 locator；uid 小的（segs[0]）应胜出
    proj.segments[1].locator = proj.segments[0].locator
    proj.segments[0].target = "FIRST"
    proj.segments[0].status = SegmentStatus.TRANSLATED
    proj.segments[1].target = "SECOND"
    proj.segments[1].status = SegmentStatus.TRANSLATED
    out = p.regenerate(proj, "x.rpy")
    # segs[0] uid 必然小于 segs[1]（文件内序号 1 < 2）
    assert "FIRST" in out and "SECOND" not in out
    # 另一行未受影响
    assert "line two" in out


def test_locator_collision_without_target_skipped(tmp_path):
    """同 locator 的第一段无译文 → 不静默落到第二段（保持原样，可发现）。"""
    p, proj, f = _make(tmp_path)
    proj.segments[1].locator = proj.segments[0].locator
    proj.segments[0].target = None          # 无译文
    proj.segments[0].status = SegmentStatus.PENDING
    proj.segments[1].target = "SECOND"
    proj.segments[1].status = SegmentStatus.TRANSLATED
    out = p.regenerate(proj, "x.rpy")
    # 第一段无译文 → 该行原样（不被 SECOND 顶替）
    assert "line one" in out
    assert "SECOND" not in out


def test_normal_case_unaffected(tmp_path):
    """正常无撞车场景：译文按各自 locator 精确写回。"""
    p, proj, f = _make(tmp_path)
    proj.segments[0].target = "第一行"
    proj.segments[0].status = SegmentStatus.TRANSLATED
    proj.segments[1].target = "第二行"
    proj.segments[1].status = SegmentStatus.TRANSLATED
    out = p.regenerate(proj, "x.rpy")
    assert 'e "第一行"' in out and 'e "第二行"' in out


def test_write_back_warns_on_duplicate_locators(tmp_path, caplog):
    """写回前对 locator 重复打警告（让用户在写回前发现异常数据）。"""
    import logging
    from pytran.unren import UnrenImpl

    game = tmp_path / "game_root"
    (game / "game" / "tl" / "chinese2").mkdir(parents=True)
    (game / "renpy").mkdir()
    f = game / "game" / "tl" / "chinese2" / "a.rpy"
    f.write_text('translate chinese t1:\n    e "hello"\n', encoding="utf-8")

    from pytran.models import Project
    segs = [
        Segment(uid=make_uid("a.rpy", 1), file="tl/chinese2/a.rpy",
                locator="# game/tl/chinese2/a.rpy:2", feature="f",
                kind="dialogue", comment="", role="", source="hello",
                target="你好", status=SegmentStatus.TRANSLATED),
        Segment(uid=make_uid("a.rpy", 2), file="tl/chinese2/a.rpy",
                locator="# game/tl/chinese2/a.rpy:2",  # 故意重复
                feature="f", kind="dialogue", comment="", role="",
                source="dup", target="重复", status=SegmentStatus.TRANSLATED),
    ]
    proj = Project(files=[GameFile(name="tl/chinese2/a.rpy",
                                   game_relpath="tl/chinese2/a.rpy")],
                   segments=segs, game_root=str(game))

    with caplog.at_level(logging.WARNING):
        UnrenImpl._warn_duplicate_locators(proj)
    assert "重复 locator" in caplog.text
