"""第 0 层：整体 JSON 数组解析（用户实测 2026-10-20 修复）。"""
import pytest

from pytran.interfaces import ProtocolError
from pytran.protocol import parse_response


def test_multiline_expanded_array_parsed():
    """模型输出多行展开的逗号分隔对象（无括号）→ 第 0 层整批回收。"""
    raw = (
        '{\n'
        '  "i": 11,\n'
        '  "t": "viewport"\n'
        '},\n'
        '{\n'
        '  "i": 12,\n'
        '  "t": "vertical"\n'
        '}'
    )
    res = parse_response(raw, [11, 12, 13], allow_keep_declare=False,
                         sources={11: "viewport", 12: "vertical"})
    assert res.results == {11: "viewport", 12: "vertical"}
    assert 13 in res.missing


def test_trailing_comma_array():
    res = parse_response('[{"i":1,"t":"a"},]', [1], sources={1: "x"})
    assert res.results == {1: "a"}


def test_jsonl_main_path_unaffected():
    res = parse_response('{"i":1,"t":"a"}\n{"i":2,"t":"b"}', [1, 2],
                         sources={1: "x", 2: "y"})
    assert res.results == {1: "a", 2: "b"}


def test_plain_text_still_raises():
    with pytest.raises(ProtocolError):
        parse_response("no numbers here", [1], sources={1: "x"})


def test_array_with_keep_declare():
    raw = '[{"i":1,"t":"Horos","k":1,"r":"name"}, {"i":2,"t":"hi"}]'
    res = parse_response(raw, [1, 2], allow_keep_declare=True,
                         sources={1: "Horos", 2: "hi"})
    assert any(hasattr(v, "reason") for v in res.results.values())


def test_fenced_no_bracket_multiline_objects():
    """BUGFIX(layer0-fence)：```json 围栏 + 无括号多行对象流 → 整批回收。

    用户实测 2026-10-20 第二例：模型输出 ```json {…},\n{…} ```，旧代码
    `raw.startswith` 被围栏字符挡住返回 None，逐行层又把对象内部碎成
    `{`/`"i": 1,` 废行，三层全灭 → 整批 ProtocolError（uid=1..20 全 missing）。
    """
    raw = (
        '```json\n'
        '{\n  "i": 1,\n  "t": "a"\n},\n'
        '{\n  "i": 2,\n  "t": "b"\n}\n'
        '```'
    )
    res = parse_response(raw, [1, 2], sources={1: "x", 2: "y"})
    assert res.results == {1: "a", 2: "b"}
    assert res.missing == []


def test_fenced_json_array():
    """标准围栏数组（```json [...] ```）→ 整批回收。"""
    res = parse_response('```json\n[{"i":1,"t":"a"}]\n```', [1],
                         sources={1: "x"})
    assert res.results == {1: "a"}


def test_bad_escape_falls_through_to_line_layer():
    """第 0 层拒非法转义（json.loads 严格），逐行层兜回收——路径不变。"""
    res = parse_response('[{"i":1,"t":"a\\qb"}]', [1], sources={1: "x"})
    assert res.results == {1: "a\\qb"}


# ---- BUGFIX(layer0-scavenger)：逐对象打捞（用户实测 2026-10-20 第三例）----
# 层级约定：第 0 层（整体 JSON，含悬空逗号清理）→ 逐行层（保住 unparsed
# 诊断）→ 打捞层（扫描平衡 {...} 片段逐个解析；仅前两层全灭时调用）。

_MULTILINE = (
    '{\n  "i": 1,\n  "t": "a"\n},\n'
    '{\n  "i": 2,\n  "t": "b"\n},\n'
    '{\n  "i": 3,\n  "t": "c"\n},\n'
    '{\n  "i": 4,\n  "t": "d"\n}'
)
_SRC4 = {i: "s" for i in [1, 2, 3, 4]}


def test_trailing_dangling_comma():
    """多行对象流末尾悬空逗号 → 打捞层整批回收。"""
    res = parse_response(_MULTILINE + ",\n", [1, 2, 3, 4], sources=_SRC4)
    assert set(res.results) == {1, 2, 3, 4}
    assert res.missing == []


def test_truncated_mid_object():
    """响应被截断（最后对象不完整）→ 完整对象回收，截断行进 missing。"""
    res = parse_response(_MULTILINE[:-1], [1, 2, 3, 4], sources=_SRC4)
    assert set(res.results) == {1, 2, 3}
    assert 4 in res.missing


def test_fenced_truncated():
    """围栏 + 截断组合 → 打捞层回收。"""
    res = parse_response("```json\n" + _MULTILINE[:-1] + "\n```",
                         [1, 2, 3, 4], sources=_SRC4)
    assert set(res.results) == {1, 2, 3}
    assert 4 in res.missing


def test_prose_wrapped_multiline_objects():
    """闲话包夹多行对象流（逐行层救不回的多行形态）→ 打捞层回收。"""
    res = parse_response("翻译如下：\n" + _MULTILINE + "\n完毕。",
                         [1, 2, 3, 4], sources=_SRC4)
    assert set(res.results) == {1, 2, 3, 4}


def test_garbage_still_raises():
    """纯闲话无任何对象 → 仍抛 ProtocolError。"""
    with pytest.raises(ProtocolError):
        parse_response("好的，以下是翻译：完毕。", [1], sources={1: "s"})
