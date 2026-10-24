"""稳定 uid 分配器 + 合并导入 uid 对位（用户裁决 2026-10-24）。

uid = 文件路径 hash(16bit) << 16 | 文件内序号(16bit)：
* 同文件同段跨导入 uid 一致 → 合并导入按 uid 直接对位，无需模糊匹配；
* 不同文件 hash 不同 → 永不撞车。

2026-10-24 事故：旧实现每次导入把 uid 从 1 重编，合并导入时新旧 uid
撞车（旧 uid=5 配到新 uid=5 完全不同的段）→ 写回后 15 个文件译文
全部张冠李戴。修复 = uid 分配稳定化，而非放弃 uid 主键。
"""
from __future__ import annotations

from pytran.models import Segment, SegmentStatus
from pytran.project import build_project, JsonProjectStore
from pytran.uids import make_uid, path_hash, assign_uids_flat


def _seg(uid: int, file: str, source: str) -> Segment:
    return Segment(
        uid=uid, file=file, locator=f"# game/{file}:{uid}",
        feature=f"translate chinese t{uid}:", kind="dialogue",
        comment="", role="", source=source,
        status=SegmentStatus.PENDING,
    )


# -- 分配器基本性质 ---------------------------------------------------------


def test_uid_stable_across_calls():
    """同 (路径, 序号) → 同 uid（跨进程稳定）。"""
    assert make_uid("a/b.rpy", 1) == make_uid("a/b.rpy", 1)
    assert path_hash("a/b.rpy") == path_hash("a/b.rpy")


def test_uid_unique_across_files():
    """不同文件 → 不同 uid 段（hash 槽不同）。"""
    uids = {make_uid(f"file{i}.rpy", 1) for i in range(50)}
    assert len(uids) == 50, "不同文件同序号不得撞车"


def test_uid_seq_stable_within_file():
    """同文件不同序号 → 不同 uid，且按序号递增有序。"""
    u = [make_uid("x.rpy", i) for i in range(1, 11)]
    assert len(set(u)) == 10
    assert u == sorted(u)


def test_uid_high_bit_distinguishes_from_legacy():
    """新 uid 恒 ≥ 1<<16（高位是路径 hash）——用于旧工程迁移检测。"""
    assert make_uid("a.rpy", 1) >= (1 << 16)


# -- build_project 分配 -----------------------------------------------------


def test_build_project_stable_uids_same_file():
    """同文件两次 build_project → 段 uid 完全一致（合并导入的前提）。"""
    f = "tl/chinese2/script.rpy"
    segs1 = [_seg(0, f, f"s{i}") for i in range(5)]
    segs2 = [_seg(999, f, f"s{i}") for i in range(5)]  # 传入 uid 被忽略
    build_project(list(segs1))
    build_project(list(segs2))
    assert [s.uid for s in segs1] == [s.uid for s in segs2]


def test_build_project_multi_file_no_collision():
    """多文件导入 uid 全局唯一（不再需要事后重编）。"""
    segs = []
    for f in ("a.rpy", "b.rpy", "c.rpy"):
        segs.extend(_seg(0, f, f"{f}-s{i}") for i in range(20))
    build_project(segs)
    uids = [s.uid for s in segs]
    assert len(set(uids)) == len(uids)


def test_build_project_preserves_file_order():
    """uid 序号 = 文件内出现顺序（与导入顺序一致，写回定位不受影响）。"""
    f = "a.rpy"
    segs = [_seg(0, f, "x"), _seg(0, f, "y"), _seg(0, f, "z")]
    build_project(segs)
    assert [s.uid for s in segs] == [
        make_uid(f, 1), make_uid(f, 2), make_uid(f, 3)]


def test_reassign_uids_opt_out():
    """reassign_uids=False 保留调用方 uid（CSV 回填同会话场景）。"""
    segs = [_seg(7, "a.rpy", "x")]
    build_project(segs, reassign_uids=False)
    assert segs[0].uid == 7


# -- 合并导入 uid 对位（核心回归） -------------------------------------------


def test_merge_import_uid_alignment():
    """核心：两次导入同文件 → uid 一致 → 旧译文按 uid 对位到正确段。

    这正是 2026-10-24 事故的反面：旧 uid=5 现在仍指向同文件第 5 段，
    不再是「新工程同号的另一段」。
    """
    f1, f2 = "tl/chinese2/common.rpy", "tl/chinese2/exp-wos.rpy"
    # 第一次导入：common + exp-wos
    first = ([_seg(0, f1, f"c{i}") for i in range(6)]
             + [_seg(0, f2, f"w{i}") for i in range(6)])
    build_project(first)
    uid_of_common_5 = first[4].uid
    uid_of_wos_3 = first[6 + 2].uid

    # 给 common 第 5 段一个可识别译文
    first[4].target = "COMMON5译文"
    first[4].status = SegmentStatus.TRANSLATED

    # 第二次导入（模拟合并导回）：重新解析同文件
    second = ([_seg(0, f1, f"c{i}") for i in range(6)]
              + [_seg(0, f2, f"w{i}") for i in range(6)])
    build_project(second)

    # uid 一致 → 对位成功
    assert second[4].uid == uid_of_common_5
    assert second[6 + 2].uid == uid_of_wos_3

    # 模拟 _merge_import 的 uid 对位
    old_translated = {s.uid: s.target for s in first
                      if s.status == SegmentStatus.TRANSLATED and s.target}
    for s in second:
        if s.uid in old_translated:
            s.target = old_translated[s.uid]
            s.status = SegmentStatus.REVIEWED

    assert second[4].target == "COMMON5译文"
    assert second[4].source == "c4"
    assert second[4].status == SegmentStatus.REVIEWED
    # 其它段不受影响
    assert second[3].target is None
    assert second[6].target is None


def test_merge_import_no_cross_file_leak():
    """合并导入不得把 A 文件的译文写到 B 文件（hash 槽隔离）。"""
    fa, fb = "tl/chinese2/a.rpy", "tl/chinese2/b.rpy"
    first = [_seg(0, fa, f"a{i}") for i in range(4)] + \
            [_seg(0, fb, f"b{i}") for i in range(4)]
    build_project(first)
    first[1].target = "A1译文"
    first[1].status = SegmentStatus.TRANSLATED

    second = [_seg(0, fa, f"a{i}") for i in range(4)] + \
             [_seg(0, fb, f"b{i}") for i in range(4)]
    build_project(second)
    old = {s.uid: s.target for s in first if s.target}
    for s in second:
        if s.uid in old:
            s.target = old[s.uid]
    # b 文件段（下标 4..7）一个都不该拿到译文
    for s in second[4:]:
        assert s.target is None, f"{s.file} 段被错配了译文"


# -- 旧工程迁移 -------------------------------------------------------------


def test_load_migrates_legacy_uids(tmp_path):
    """旧格式 project.json（1..N 顺序 uid）加载时迁移为稳定 uid。"""
    legacy = {
        "version": 3, "app": "pytran",
        "files": [{"name": "a.rpy", "game_relpath": "a.rpy"}],
        "next_uid": 4,
        "segments": [
            {"uid": 1, "file": "a.rpy", "locator": "# game/a.rpy:1",
             "feature": "f", "kind": "dialogue", "comment": "", "role": "",
             "source": "s1", "target": "T1", "status": "translated",
             "attempts": 0, "error": None, "indent": 4},
            {"uid": 2, "file": "a.rpy", "locator": "# game/a.rpy:2",
             "feature": "f", "kind": "dialogue", "comment": "", "role": "",
             "source": "s2", "target": None, "status": "pending",
             "attempts": 0, "error": None, "indent": 4},
        ],
        "term_profile": "profile.json", "game_root": "",
    }
    import json
    (tmp_path / "project.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    loaded = JsonProjectStore().load(tmp_path)
    assert [s.uid for s in loaded.segments] == [
        make_uid("a.rpy", 1), make_uid("a.rpy", 2)]
    assert loaded.segments[0].target == "T1"  # 内容不丢
