"""全文复核（``pytran.review``）纯逻辑测试。

锁定行为（用户 2026-10-24 决策，取代 V7 拟声词/编号错配启发式）：

* 提示词逐行给出 ``uid<TAB>原文<TAB>译文``，字段内的换行/TAB 被压平；
* 回执**逐行**：``uid|ok`` / ``uid|NG|原因``，无回执的行进入 ``unconfirmed``
  （「模型什么都不报」是本设计最大风险，必须能识别出来）；
* 窗口切块、未确认行减半重发、单窗口请求失败不毁全局；
* 停止钩子生效。
"""
from __future__ import annotations

from pytran.review import (
    REVIEW_SYSTEM_PROMPT,
    ReviewRow,
    ReviewRunner,
    build_review_prompt,
    build_rows,
    chunk_rows,
    parse_review_response,
)
from pytran.models import Segment, SegmentStatus


def _row(uid: int, source: str, target: str) -> ReviewRow:
    return ReviewRow(uid=uid, source=source, target=target, file="a.rpy")


def _seg(uid: int, source: str, target: str,
         status: SegmentStatus = SegmentStatus.TRANSLATED) -> Segment:
    return Segment(uid=uid, file="a.rpy", locator=f"a.rpy:{uid}",
                   feature=f"translate chinese s{uid}:", kind="dialogue",
                   comment="", role="", source=source, target=target,
                   status=status)


# -- 提示词 ---------------------------------------------------------------


def test_prompt_lists_every_row_with_tabs():
    prompt = build_review_prompt([_row(1, "Hello", "你好"),
                                  _row(2, "Bye", "再见")])
    assert "1\tHello\t你好" in prompt
    assert "2\tBye\t再见" in prompt
    assert "每一个编号" in prompt


def test_prompt_flattens_newlines_and_tabs():
    prompt = build_review_prompt([_row(7, "line1\nline2", "第一行\t第二行")])
    # 行内换行压成字面 \n，TAB 换成空格——否则会破坏「一行一编号」结构
    assert "line1\\nline2" in prompt
    assert "第一行 第二行" in prompt
    body = prompt.split("----- 待校对内容开始 -----")[1]
    body = body.split("----- 待校对内容结束 -----")[0]
    assert len([ln for ln in body.splitlines() if ln.strip()]) == 1


def test_system_prompt_demands_verdict_only():
    assert "逐行" in REVIEW_SYSTEM_PROMPT
    assert "不要解释" in REVIEW_SYSTEM_PROMPT


# -- 回执解析 -------------------------------------------------------------


def test_parse_ok_and_ng():
    text = "1|ok\n2|NG|译文是别的行的内容"
    got = parse_review_response(text, [1, 2])
    assert got[1] is None
    assert got[2] == "译文是别的行的内容"


def test_parse_missing_line_is_absent_not_ok():
    got = parse_review_response("1|ok", [1, 2])
    assert 1 in got
    assert 2 not in got  # 未确认——不能当成 ok


def test_parse_ng_without_reason_gets_default():
    got = parse_review_response("5|NG", [5])
    assert got[5]  # 有兜底原因，不是 None


def test_parse_tolerates_noise_and_fullwidth():
    text = ("好的，下面是结论：\n"
            "```\n"
            "1｜ok\n"
            "- 2: NG：漏译\n"
            "```\n")
    got = parse_review_response(text, [1, 2])
    assert got[1] is None
    assert "漏译" in got[2]


def test_parse_filters_unknown_uids():
    got = parse_review_response("1|ok\n99|NG|x", [1])
    assert set(got) == {1}


def test_parse_empty_returns_nothing():
    assert parse_review_response("", [1, 2]) == {}
    assert parse_review_response("我无法完成该任务。", [1, 2]) == {}


# -- 行挑选与切块 ---------------------------------------------------------


def test_build_rows_skips_pending_without_target():
    rows = build_rows([
        _seg(1, "Pending.", "", SegmentStatus.PENDING),
        _seg(2, "Done.", "已完成。"),
    ])
    assert [r.uid for r in rows] == [2]


def test_build_rows_keeps_translated_but_empty_target():
    rows = build_rows([_seg(3, "Something.", "", SegmentStatus.TRANSLATED)])
    assert [r.uid for r in rows] == [3]  # 空译文交给模型判漏译


def test_chunk_rows_splits_by_window():
    rows = [_row(i, f"s{i}", f"t{i}") for i in range(1, 8)]
    chunks = chunk_rows(rows, 3)
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert chunk_rows(rows, 0) == [rows]  # 0 = 不切
    assert chunk_rows([], 10) == []


# -- 编排 -----------------------------------------------------------------


def test_runner_collects_ng_and_unconfirmed():
    rows = [_row(1, "a", "甲"), _row(2, "b", "乙")]

    def call(prompt, system="", max_tokens=None, ids=None, line_count=0):
        assert ids == [1, 2]
        return "1|ok\n2|NG|译文与原文无关"

    res = ReviewRunner(call, window_lines=10).run(rows)
    assert [i.uid for i in res.issues] == [2]
    assert res.unconfirmed == []
    assert res.windows == 1 and res.rows == 2


def test_runner_marks_omitted_lines_unconfirmed():
    rows = [_row(1, "a", "甲"), _row(2, "b", "乙")]

    # 永远只回第一行 → 第二行应当被识别为「未确认」而不是 ok
    def call(prompt, system="", max_tokens=None, ids=None, line_count=0):
        return "1|ok"

    res = ReviewRunner(call, window_lines=10, max_rounds=1).run(rows)
    assert res.issues == []
    assert res.unconfirmed == [2]


def test_runner_retries_unconfirmed_with_smaller_windows():
    """首轮整窗偷懒 → 减半重发后补齐（模拟输出截断）。"""
    calls: list[list[int]] = []

    def call(prompt, system="", max_tokens=None, ids=None, line_count=0):
        calls.append(list(ids))
        if len(ids) > 2:
            return "\n".join(f"{u}|ok" for u in ids[:1])  # 截断：只回一行
        return "\n".join(f"{u}|ok" for u in ids)

    rows = [_row(i, f"s{i}", f"t{i}") for i in range(1, 9)]
    res = ReviewRunner(call, window_lines=8, max_rounds=3).run(rows)
    assert res.unconfirmed == []
    assert len(calls) > 1  # 确实重发了


def test_runner_window_failure_does_not_kill_run():
    rows = [_row(1, "a", "甲"), _row(2, "b", "乙")]

    def call(prompt, system="", max_tokens=None, ids=None, line_count=0):
        if 1 in ids:
            raise RuntimeError("boom")
        return "2|ok"

    res = ReviewRunner(call, window_lines=1, max_rounds=1).run(rows)
    assert res.errors and "boom" in res.errors[0]
    assert res.unconfirmed == [1]
    assert res.issues == []


def test_runner_honours_stop_hook():
    rows = [_row(i, f"s{i}", f"t{i}") for i in range(1, 5)]
    state = {"stop": True}

    def call(prompt, system="", max_tokens=None, ids=None, line_count=0):
        return "\n".join(f"{u}|ok" for u in ids)

    res = ReviewRunner(call, window_lines=1,
                       should_stop=lambda: state["stop"]).run(rows)
    assert res.errors and "停止" in res.errors[0]


def test_runner_empty_rows_short_circuits():
    def call(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("空输入不该发请求")

    res = ReviewRunner(call).run([])
    assert res.issues == [] and res.unconfirmed == [] and res.rows == 0


def test_runner_reports_progress():
    seen: list[dict] = []
    rows = [_row(1, "a", "甲")]

    def call(prompt, system="", max_tokens=None, ids=None, line_count=0):
        return "1|ok"

    ReviewRunner(call, window_lines=10,
                 on_progress=seen.append).run(rows)
    assert seen and seen[-1]["confirmed"] == 1


def test_runner_passes_max_tokens_and_line_count():
    captured: dict = {}

    def call(prompt, system="", max_tokens=None, ids=None, line_count=0):
        captured.update(max_tokens=max_tokens, line_count=line_count)
        return "1|ok"

    ReviewRunner(call, window_lines=10, max_tokens=4096).run([_row(1, "a", "甲")])
    assert captured == {"max_tokens": 4096, "line_count": 1}
