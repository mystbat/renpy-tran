"""uid 分配器测试（v3.1 降级版：会话内 1..N 序号）。

历史沿革（保留勿删，防重蹈）：
* 2026-10-24 事故：合并导入按 uid 对位，但 uid 每次导入从 1 重编 →
  新旧 uid 撞车 → 写回后 15 文件译文全部张冠李戴。
* 2026-10-25 b13ce9e：改为「路径 hash + 段序号」，声称"永不撞车"，
  实测 16-bit hash 在 3000 路径上 59 次碰撞——未测量的断言。
* 2026-10-26 v3.1（segkey-identity）：跨导入身份改由段键 (file, source)
  承担（pytran.identity），uid 降级为会话内序号，不再参与任何匹配。

原 hash-uid 断言（跨文件唯一、高位区分 legacy、make_uid 稳定等）
全部随方案退役而改写。
"""
from __future__ import annotations

import json

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
    """兼容语义：make_uid/path_hash 保留为确定性纯函数（返回退化为序号/0）。"""
    assert make_uid("a/b.rpy", 1) == make_uid("a/b.rpy", 1) == 1
    assert path_hash("a/b.rpy") == path_hash("a/b.rpy")


def test_assign_uids_flat_is_sequential():
    """会话内序号：按出现顺序 1..N，文档顺序即 uid 顺序。"""
    segs = [_seg(0, "a.rpy", "x"), _seg(0, "b.rpy", "y"), _seg(0, "a.rpy", "z")]
    assign_uids_flat(segs)
    assert [s.uid for s in segs] == [1, 2, 3]


# -- build_project 分配 -----------------------------------------------------


def test_build_project_stable_uids_same_file():
    """同内容两次 build_project → 段 uid 完全一致（同顺序 → 同序号）。"""
    f = "tl/chinese2/script.rpy"
    segs1 = [_seg(0, f, f"s{i}") for i in range(5)]
    segs2 = [_seg(999, f, f"s{i}") for i in range(5)]  # 传入 uid 被忽略
    build_project(list(segs1))
    build_project(list(segs2))
    assert [s.uid for s in segs1] == [s.uid for s in segs2]


def test_build_project_multi_file_no_collision():
    """多文件导入 uid 全局唯一（连续编号 1..N）。"""
    segs = []
    for f in ("a.rpy", "b.rpy", "c.rpy"):
        segs.extend(_seg(0, f, f"{f}-s{i}") for i in range(20))
    build_project(segs)
    uids = [s.uid for s in segs]
    assert len(set(uids)) == len(uids)


def test_build_project_preserves_file_order():
    """uid 序号 = 文档出现顺序（segmenter 批次随之回到真实阅读顺序）。"""
    f = "a.rpy"
    segs = [_seg(0, f, "x"), _seg(0, f, "y"), _seg(0, f, "z")]
    build_project(segs)
    assert [s.uid for s in segs] == [1, 2, 3]


def test_reassign_uids_opt_out():
    """reassign_uids=False 保留调用方 uid（CSV 回填同会话场景）。"""
    segs = [_seg(7, "a.rpy", "x")]
    build_project(segs, reassign_uids=False)
    assert segs[0].uid == 7


# -- 合并导入对位（核心回归：对位不再依赖 uid，但 uid 一致时旧行为不变） ------


def test_merge_import_uid_alignment():
    """两次导入同文件同内容 → uid 一致；按 uid 对位（旧行为兼容）仍正确。

    v3.1 后 _merge_import 实际走身份对位（file, source），本用例保留
    以证明"内容不变时"两种语义结果一致——正常路径零变化。
    """
    f1, f2 = "tl/chinese2/common.rpy", "tl/chinese2/exp-wos.rpy"
    first = ([_seg(0, f1, f"c{i}") for i in range(6)]
             + [_seg(0, f2, f"w{i}") for i in range(6)])
    build_project(first)
    uid_of_common_5 = first[4].uid
    uid_of_wos_3 = first[6 + 2].uid

    first[4].target = "COMMON5译文"
    first[4].status = SegmentStatus.TRANSLATED

    second = ([_seg(0, f1, f"c{i}") for i in range(6)]
              + [_seg(0, f2, f"w{i}") for i in range(6)])
    build_project(second)

    assert second[4].uid == uid_of_common_5
    assert second[6 + 2].uid == uid_of_wos_3

    old_translated = {s.uid: s.target for s in first
                      if s.status == SegmentStatus.TRANSLATED and s.target}
    for s in second:
        if s.uid in old_translated:
            s.target = old_translated[s.uid]
            s.status = SegmentStatus.REVIEWED

    assert second[4].target == "COMMON5译文"
    assert second[4].source == "c4"
    assert second[4].status == SegmentStatus.REVIEWED
    assert second[3].target is None
    assert second[6].target is None


def test_merge_import_no_cross_file_leak():
    """合并导入不得把 A 文件的译文写到 B 文件（身份含文件维度）。"""
    fa, fb = "tl/chinese2/a.rpy", "tl/chinese2/b.rpy"
    from pytran.identity import resolve_batch

    first = [_seg(0, fa, f"a{i}") for i in range(4)] + \
            [_seg(0, fb, f"b{i}") for i in range(4)]
    build_project(first)
    first[1].target = "A1译文"
    first[1].status = SegmentStatus.TRANSLATED

    second = [_seg(0, fa, f"a{i}") for i in range(4)] + \
             [_seg(0, fb, f"b{i}") for i in range(4)]
    build_project(second)

    rows = [type("R", (), {"file": s.file, "locator": s.locator,
                           "feature": s.feature, "source": s.source,
                           "target": s.target})()
            for s in first if s.target]
    out = resolve_batch(second, rows)
    assert len(out.matches) == 1
    assert out.matches[0].segment.file == fa
    # b 文件段一个都不该拿到译文
    for s in second[4:]:
        assert s.target is None, f"{s.file} 段被错配了译文"


# -- 旧工程迁移 -------------------------------------------------------------


def test_load_migrates_legacy_hash_uids(tmp_path):
    """旧格式 project.json（路径 hash 大整数 uid）加载时重编为 1..N。"""
    legacy = {
        "version": 3, "app": "pytran",
        "files": [{"name": "a.rpy", "game_relpath": "a.rpy"}],
        "next_uid": 4,
        "segments": [
            {"uid": 1130168321, "file": "a.rpy", "locator": "# game/a.rpy:1",
             "feature": "f", "kind": "dialogue", "comment": "", "role": "",
             "source": "s1", "target": "T1", "status": "translated",
             "attempts": 0, "error": None, "indent": 4},
            {"uid": 1130168322, "file": "a.rpy", "locator": "# game/a.rpy:2",
             "feature": "f", "kind": "dialogue", "comment": "", "role": "",
             "source": "s2", "target": None, "status": "pending",
             "attempts": 0, "error": None, "indent": 4},
        ],
        "term_profile": "profile.json", "game_root": "",
    }
    (tmp_path / "project.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    loaded = JsonProjectStore().load(tmp_path)
    assert [s.uid for s in loaded.segments] == [1, 2]
    assert loaded.next_uid == 3
    assert loaded.segments[0].target == "T1"  # 内容不丢
