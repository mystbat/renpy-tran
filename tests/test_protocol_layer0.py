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
