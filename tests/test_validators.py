"""V1–V6 校验、自动修复与四态漏翻检测器测试（设计文档 §5、ADR-0001）。

覆盖 §11 表格中 `test_validators` 一行要求的：标签增删/插值丢失/引号/白名单豁免样例，
并额外覆盖 §5.3 四态判定、ADR-0001 的固定判定顺序与「检测器是纯函数」约束。
"""
from __future__ import annotations

import pytest

from pytran.config import TermProfile
from pytran.interfaces import Detection
from pytran.validators import (
    TAG_RE,
    VAR_RE,
    ESC_RE,
    CheckResult,
    Validator,
    Whitelist,
    compute_residual,
    is_interjection_like,
    repair_quotes,
    strip_fences,
    tag_messages,
    tags_ok,
)


# ---------------------------------------------------------------------------
# V3：代码围栏与首尾空白剥离（§5 表 V3；§5.2 「.rstrip() 只对 V3 使用」）
# ---------------------------------------------------------------------------

def test_v3_strip_fenced_block():
    assert strip_fences('```json\n{"i": 1, "t": "你好"}\n```') == '{"i": 1, "t": "你好"}'


def test_v3_strip_inline_fence():
    assert strip_fences('```{"i": 1, "t": "你好"}```') == '{"i": 1, "t": "你好"}'


def test_v3_strip_surrounding_whitespace_only():
    # 首尾空白剥离；正文内部空白绝不动（§5.2）
    assert strip_fences("  你好  世界  ") == "你好  世界"
    assert strip_fences("\n\t你好\n") == "你好"


def test_v3_no_fence_keeps_internal_newline():
    assert strip_fences("a\nb") == "a\nb"


# ---------------------------------------------------------------------------
# V4：英文双引号交替替换为「」/『』（§5 表 V4，沿用 2.0 check-UI 算法）
# ---------------------------------------------------------------------------

def test_v4_quote_alternation():
    assert repair_quotes('他说"你好"') == "他说「你好」"


def test_v4_quote_two_pairs():
    assert repair_quotes('"a" and "b"') == "「a」 and 「b」"


def test_v4_quote_nested_uses_double_brackets():
    # 嵌套引号用『』，与外层「」区分
    assert repair_quotes('"外层 "内层" 继续"') == "「外层 『内层』 继续」"


def test_v4_quote_no_quote_is_identity():
    assert repair_quotes("没有引号") == "没有引号"


def test_v4_quote_repair_inside_check_leaves_no_ascii_quote():
    v = Validator()
    _, repaired = v.check('He said "hi".', '他说"嗨"。', TermProfile())
    assert '"' not in repaired
    assert repaired == "他说「嗨」。"


# ---------------------------------------------------------------------------
# V6：标签/插值/转义计数（§5.2）
# ---------------------------------------------------------------------------

def test_v6_tags_ok_identical():
    assert tags_ok("Wait{w} for [player]!", "等一下{w}吧 [player]！") is True


def test_v6_tags_ok_reordered_is_ok():
    # 计数相等而非顺序相等（§5.2：避免对语序调整误报）
    assert tags_ok("{a}{b} x", "x {b}{a}") is True


def test_v6_tag_dropped_is_error():
    assert tags_ok("Wait{w} now", "等等吧") is False


def test_v6_tag_added_is_error():
    assert tags_ok("Wait now", "等等{w}吧") is False


def test_v6_var_dropped_is_error():
    assert tags_ok("Hi [player_name]", "你好") is False


def test_v6_t_modifier_is_same_tag():
    # Ren'Py 的 `!t` 翻译修饰符只用于方括号变量标签，不改变其身份：
    # [e_name] ≡ [e_name!t]
    assert tags_ok("[e_name!t] 你好", "[e_name] 你好") is True
    msgs = tag_messages("[e_name!t] 你好", "[e_name] 你好")
    assert msgs == []  # 不误报标签改动
    # 花括号文本标签 {w} 没有 !t 写法，保持原样比较
    assert tags_ok("{w!t} x", "{w} x") is False
    # 真正的标签改动仍应报错
    assert tags_ok("[e_name!t] 你好", "你好") is False


def test_v6_escape_count():
    src = r'He said \"hi\"\n'
    assert tags_ok(src, r'他说\"嗨\"\n') is True
    assert tags_ok(src, r'他说\"嗨\"') is False


def test_v6_check_reports_tag_error_with_counts():
    v = Validator()
    result = v.check_detailed("Wait{w} for [player]!", "等一下 [player]！", TermProfile())
    assert result.detection != Detection.PASS
    assert any("{w}" in e for e in result.errors)


def test_v6_tag_messages_format():
    msgs = tag_messages("Wait{w}{w} now", "等等{w}吧")
    assert msgs and msgs[0].startswith("tags:")
    assert "原文2处译文1处" in msgs[0]


def test_tag_var_escape_regexes_from_design():
    assert TAG_RE.findall("{w} {b} {color=#fff}") == ["{w}", "{b}", "{color=#fff}"]
    assert VAR_RE.findall("[player_name] [config.name!t]") == ["[player_name]", "[config.name!t]"]
    assert ESC_RE.findall(r"\n\"\\") == [r"\n", r"\"", r"\\"]


# ---------------------------------------------------------------------------
# V5 / §5.1：白名单豁免
# ---------------------------------------------------------------------------

def test_v5_term_key_is_exempt():
    v = Validator()
    profile = TermProfile(terms={"Ian": "伊恩"})
    detection, repaired = v.check("Ian smiled.", "Ian 笑了。", profile)
    assert detection == Detection.PASS
    assert repaired == "Ian 笑了。"  # 无修复需求


def test_v5_exempt_english_list_is_exempt():
    v = Validator()
    profile = TermProfile(exempt_english=["Stalkfap"])
    detection, _ = v.check("Open Stalkfap.", "打开 Stalkfap。", profile)
    assert detection == Detection.PASS


def test_v5_unknown_english_is_residual():
    v = Validator()
    detection, _ = v.check("Open the door.", "打开 door。", TermProfile())
    assert detection == Detection.RESIDUAL


def test_v5_english_appearing_in_source_is_exempt():
    # §5.1：该英文片段同样出现在原文中且为整词 → 豁免
    v = Validator()
    detection, _ = v.check("Press E to interact.", "按 E 交互。", TermProfile())
    assert detection == Detection.PASS


def test_v5_mixed_row_still_queued_but_preserved_reported():
    # ADR-0001：混合行（专名+真漏翻）仍入队，但保留项要报出来供重发请求注明
    v = Validator()
    profile = TermProfile(terms={"Ian": "伊恩"})
    result = v.check_detailed("Ian said hello", "Ian 说 hello", profile)
    assert result.detection == Detection.RESIDUAL
    assert "Ian" in result.preserved
    assert result.residual == ["hello"]


def test_v5_short_abbrev_switch_default_off():
    v = Validator()
    detection, _ = v.check("Are you OK?", "你 OK 吗？", TermProfile())
    assert detection == Detection.RESIDUAL


def test_v5_short_abbrev_switch_enabled():
    v = Validator(allow_short_abbrev=True)
    detection, _ = v.check("Are you OK?", "你 OK 吗？", TermProfile())
    assert detection == Detection.PASS


def test_v5_residual_and_preserved_are_pure_outputs():
    wl = Whitelist.from_profile(TermProfile(terms={"Ian": "伊恩"}))
    residual, preserved = compute_residual("Ian left.", "Ian 离开了", wl)
    assert residual == []
    assert preserved == ["Ian"]


def test_v5_terms_take_priority_over_generic_exempt():
    wl = Whitelist(terms=("Ian",), exempt=("Ian",), allow_short_abbrev=False)
    residual, preserved = compute_residual("x", "Ian", wl)
    assert residual == []
    assert preserved == ["Ian"]


def test_v5_structural_parts_are_not_residual():
    # 标签/插值/转义本就是保留项，ADR-0001 第一步即剥离
    wl = Whitelist.from_profile(TermProfile())
    residual, _ = compute_residual("A{w}", "中文{w}[var]\\n", wl)
    assert residual == []


# ---------------------------------------------------------------------------
# §5.3 四态判定
# ---------------------------------------------------------------------------

def test_detection_pass_normal_translation():
    v = Validator()
    detection, repaired = v.check("Hello there.", "你好。", TermProfile())
    assert detection == Detection.PASS
    assert repaired == "你好。"


def test_detection_empty_for_blank_target():
    v = Validator()
    detection, _ = v.check("Hello there.", "   ", TermProfile())
    assert detection == Detection.EMPTY


def test_detection_empty_for_empty_target():
    v = Validator()
    detection, _ = v.check("Hello there.", "", TermProfile())
    assert detection == Detection.EMPTY
    result = v.check_detailed("Hello there.", "", TermProfile())
    assert "empty" in result.errors


def test_detection_untranslated_when_target_equals_source():
    v = Validator()
    detection, _ = v.check("Hello there.", "Hello there.", TermProfile())
    assert detection == Detection.UNTRANSLATED


def test_detection_pass_when_source_is_pure_punctuation():
    # §5.3：target==source 但剔除豁免后无实词（纯标点/符号）→ PASS
    v = Validator()
    detection, _ = v.check("...", "...", TermProfile())
    assert detection == Detection.PASS


def test_detection_pass_when_source_is_symbol_only():
    v = Validator()
    detection, _ = v.check("?!", "?!", TermProfile())
    assert detection == Detection.PASS


def test_detection_untranslated_when_identical_but_exempt_word_only():
    # 整行合法英文（命中术语表）→ PASS，不入队列（ADR-0001）
    v = Validator()
    profile = TermProfile(terms={"Stalkfap": " stalkfap "})
    detection, _ = v.check("Stalkfap", "Stalkfap", profile)
    assert detection == Detection.PASS


def test_detection_residual_for_partial_english():
    v = Validator()
    detection, _ = v.check("Hello there.", "你好 there。", TermProfile())
    assert detection == Detection.RESIDUAL


def test_detection_values_are_str_enum():
    # detection 直接用于引擎五态路由与 project 序列化
    assert Detection.PASS == "pass"
    assert isinstance(Detection.RESIDUAL, str)


def test_structural_error_never_passes():
    # 即使没有英文残余，标签错误也不得放行
    v = Validator()
    result = v.check_detailed("Wait{w}", "等等", TermProfile())
    assert result.detection != Detection.PASS
    assert result.errors


# ---------------------------------------------------------------------------
# §5.3 边界情形：拟声/感叹行的 profile 级开关
# ---------------------------------------------------------------------------

def test_interjection_like_detection():
    assert is_interjection_like("Hahhh...") is True
    assert is_interjection_like("Ugh!") is True
    assert is_interjection_like("Hello there") is False


def test_interjection_default_goes_to_queue():
    v = Validator()
    detection, _ = v.check("Hahhh...", "Hahhh...", TermProfile())
    assert detection == Detection.UNTRANSLATED


def test_interjection_switch_pass_via_profile():
    # DECISION(§5.3)：TermProfile 已冻结，开关借 prompt_overrides 传递
    profile = TermProfile(prompt_overrides={"interjection": "pass"})
    v = Validator()
    detection, _ = v.check("Hahhh...", "Hahhh...", profile)
    assert detection == Detection.PASS


def test_interjection_switch_does_not_affect_normal_rows():
    profile = TermProfile(prompt_overrides={"interjection": "pass"})
    v = Validator()
    detection, _ = v.check("Hello there my friend", "Hello there my friend", profile)
    assert detection == Detection.UNTRANSLATED


# ---------------------------------------------------------------------------
# 纯函数性（任务约束：不读全局配置，白名单作为参数传入）
# ---------------------------------------------------------------------------

def test_detector_is_pure_and_does_not_mutate_profile(monkeypatch):
    profile = TermProfile(terms={"Ian": "伊恩"})
    v = Validator()
    src, tgt = "Ian left.", "Ian 离开了。"

    first = v.check_detailed(src, tgt, profile)
    assert first.detection == Detection.PASS

    # 1) 不修改传入的 profile
    assert profile.terms == {"Ian": "伊恩"}

    # 2) 同输入同输出（无隐藏状态）
    assert v.check_detailed(src, tgt, profile).detection == first.detection

    # 3) 全局环境/配置变化不影响判定（检测器不读全局）
    monkeypatch.setenv("TL_API_KEY", "sk-should-not-matter")
    assert v.check_detailed(src, tgt, profile).detection == Detection.PASS


def test_check_returns_detection_and_repaired_tuple():
    v = Validator()
    out = v.check("Hello there.", "你好。", TermProfile())
    assert isinstance(out, tuple) and len(out) == 2
    detection, repaired = out
    assert detection == Detection.PASS
    assert repaired == "你好。"


def test_check_result_shape():
    result = CheckResult(detection=Detection.PASS, repaired="x")
    assert result.errors == []
    assert result.residual == []
    assert result.preserved == []


def test_validator_satisfies_runtime_protocol():
    from pytran.interfaces import Validator as ValidatorProtocol

    assert isinstance(Validator(), ValidatorProtocol)
