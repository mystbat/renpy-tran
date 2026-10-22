"""Ren'Py ``.rpy`` ↔ :class:`Segment` lossless parsing and regeneration.

Implements :class:`pytran.interfaces.Parser` for the W1 "解析链"
(parse + chunk + store). Parsing strategy (设计 §6.1, §14):

* `parse_file` walks an ``.rpy`` file **line by line**, never serialising the
  whole tree, so unrecognised blocks are kept verbatim (设计 §14 风险表:
  宁可不翻不可弄坏) and counted as ``skipped`` when they cannot be reduced to
  translatable segments.
* For every recognised translatable ``translate`` / ``translate <lang>`` /
  ``translate <lang> strings`` / ``old`` / ``new`` block we emit a
  :class:`Segment` and cache the *exact original source lines* (including
  their terminators) in ``self._lines_by_file`` keyed by file name.
* `regenerate` rebuilds a file from its cached original lines, replacing
  **only** the quoted dialogue text inside a line when the matching segment
  has a ``target``. Prefix, indentation, role prefix, quote style, trailing
  comment and the line terminator are reproduced verbatim, which guarantees a
  byte-exact round trip for untranslated content regardless of newline style
  (CRLF/LF) or BOM.

No translation logic lives here; only extraction and re-insertion
(设计 §2.1, §2.2, §3.1).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import Project, Segment, SegmentStatus

logger = logging.getLogger(__name__)

# --- block header / statement patterns -------------------------------------

# translate chinese chapter_two_6901eeea:   (label may be strings/old/new)
_TRANSLATE_RE = re.compile(
    r"^(?P<indent>\s*)translate(?:\s+(?P<lang>[\w-]+))?\s+(?P<label>[\w.]+)\s*:\s*$"
)
# bare `strings:` header (rare; usually wrapped in `translate ... strings:`)
_STRINGS_RE = re.compile(r"^(?P<indent>\s*)strings\s*:\s*$")
# generic top-level "defining" block header we do not translate (label/screen/
# init python/transform/style/...). Kept verbatim + counted as skipped (§14).
_BLOCK_HEADER_RE = re.compile(
    r"^(?P<indent>\s*)(?P<head>[\w.\-]+(?:\s+[\w.\-]+)*)\s*:\s*$"
)
# legacy `old:` / `new:` sub-headers inside a strings block
_OLD_NEW_HEADER_RE = re.compile(r"^(?P<indent>\s*)(?P<kind>old|new)\s*:\s*$")
# modern `old "X"` / `new "Y"` pair lines inside a strings block
_OLD_NEW_PAIR_RE = re.compile(
    r'^(?P<indent>\s*)(?P<kind>old|new)\s+(?P<quote>["\'])(?P<text>.*?)(?P=quote)\s*$'
)
# `old "..."` / `link "..."` reference inside a dialogue (`translate`) block
_LINK_RE = re.compile(
    r'^(?P<indent>\s*)(?P<kind>old|link)\s+(?P<quote>["\'])(?P<text>.*?)(?P=quote)\s*$'
)


def _quoted_spans(line: str) -> list[tuple[int, int, str]]:
    """Return the top-level (open, close, quote) spans of quoted strings in
    *line*, honouring backslash escapes and stopping at a ``#`` comment.

    The dialogue text is the *last* span (a role prefix such as
    ``"Eileen" "Hi"`` precedes the actual line).
    """
    spans: list[tuple[int, int, str]] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c in ('"', "'"):
            q = c
            j = i + 1
            while j < n:
                if line[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if line[j] == q:
                    spans.append((i, j, q))
                    i = j + 1
                    break
                j += 1
            else:
                i += 1
                continue
        elif c == "#":
            break  # rest is a comment, no further string
        else:
            i += 1
    return spans


def _extract_dialogue(line: str) -> tuple[str, str, str, str] | None:
    """Return ``(role, quote, text, suffix)`` for a dialogue/string body line.

    ``role`` is the speaker/character prefix (``""`` when none), ``quote`` the
    opening quote char, ``text`` the content between the quotes, and
    ``suffix`` everything after the closing quote (comment / whitespace).
    Returns ``None`` when the line holds no top-level quoted string.
    """
    spans = _quoted_spans(line)
    if not spans:
        return None
    open_idx, close_idx, quote = spans[-1]
    prefix = line[:open_idx]
    role = prefix.strip()
    text = line[open_idx + 1 : close_idx]
    suffix = line[close_idx + 1 :]
    return role, quote, text, suffix


def _is_string_body(line: str) -> bool:
    """True when *line* is a single balanced quoted literal (role-free)."""
    s = line.strip()
    if len(s) < 2 or s[0] not in "\"'" or s[-1] not in "\"'":
        return False
    return s.count(s[0]) == 2


def _body_owner(
    lines: list[str], body_idx: int, base_indent: int
) -> str:
    """返回字符串体所属的 legacy 子块头（"old" / "new" / ""）。

    从 body 行向上扫，找到第一个缩进等于 ``base_indent`` 的非空行——
    即该字符串体所在子块的头（``old:`` 或 ``new:``）。找不到返回 ""。
    （BUGFIX(old-new-pair) 配套。）
    """
    i = body_idx - 1
    while i >= 0:
        cur = lines[i]
        if not cur.strip():
            i -= 1
            continue
        ind = len(cur) - len(cur.lstrip())
        if ind < base_indent:
            m = _OLD_NEW_HEADER_RE.match(cur)
            if m is not None and ind + 4 >= base_indent:
                return m.group("kind")
            break
        i -= 1
    return ""


def _body_belongs_to_old(lines: list[str], body_idx: int, base_indent: int) -> bool:
    return _body_owner(lines, body_idx, base_indent) == "old"


def _body_belongs_to_new(lines: list[str], body_idx: int, base_indent: int) -> bool:
    return _body_owner(lines, body_idx, base_indent) == "new"


_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\u3040-\u30ff]")


def _has_cjk(text: str) -> bool:
    """文本含 CJK 表意文字/假名（BUGFIX(unrpyc-translated) 判据）。

    用于区分 unrpyc 产物中「已译中文台词」（不入待翻队列）与
    「英文原文台词」（正常送翻）。标点/全角符号不算——避免中文标点
    残留的未译行被误判为已译。
    """
    return bool(_CJK_RE.search(text))


def _replace_dialogue(line: str, new_text: str) -> str:
    """Replace the dialogue text of *line* with *new_text*, preserving the
    role prefix, quote style and trailing suffix byte-for-byte (设计 §6.1)."""
    spans = _quoted_spans(line)
    if not spans:
        return line
    open_idx, close_idx, _quote = spans[-1]
    return line[: open_idx + 1] + new_text + line[close_idx:]


def _split_keepends(line: str) -> tuple[str, str]:
    """Split a line kept with its terminator into (content, terminator)."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


class RenpyParser:
    """Line-oriented ``.rpy`` parser satisfying :class:`Parser` (设计 §3, §6.1).

    A single instance is reusable across files; each ``parse_file`` call
    caches that file's original lines so a later ``regenerate`` rebuilds it
    byte-for-byte from the same source.
    """

    def __init__(self, target_lang: str | None = None) -> None:
        # DECISION(§3.1): we extract every translatable block and record its
        # language inside `feature`; the translation *target* language is a W4
        # concern and defaults to "chinese" only as a soft hint.
        self.target_lang = target_lang
        self._lines_by_file: dict[str, list[str]] = {}
        self._bom_by_file: dict[str, bool] = {}
        self._file: str = ""

    # -- public API ---------------------------------------------------------

    def parse_file(self, path: Path, game_relpath: str = "") -> list[Segment]:
        """Parse *path* into :class:`Segment` objects (设计 §2.1, §3.1)."""
        raw = Path(path).read_bytes()
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        self._file = game_relpath or Path(path).name
        self._lines_by_file[self._file] = text.splitlines(keepends=True)
        self._bom_by_file[self._file] = had_bom
        segments = self._parse_lines(self._lines_by_file[self._file])
        return segments

    def regenerate(self, project: Project, file_name: str) -> str:
        """Rebuild *file_name*'s source from cached original lines (设计 §6.1).

        Returns the full file text. Untranslated source lines are reproduced
        verbatim; translated dialogue lines have only their quoted text
        swapped. Unparseable / skipped blocks are emitted exactly as parsed.
        """
        lines = self._lines_by_file.get(file_name)
        if not lines:
            logger.warning(
                "regenerate(%s) called before parse_file for this instance; "
                "returning empty text",
                file_name,
            )
            return ""
        line_for: dict[tuple[str, str], Segment] = {
            (seg.file, seg.locator): seg
            for seg in project.segments
            if seg.file == file_name
        }
        out: list[str] = []
        for idx, original in enumerate(lines):
            content, term = _split_keepends(original)
            locator = f"# game/{self._file}:{idx + 1}"
            seg = line_for.get((file_name, locator))
            if seg is not None and seg.target:
                out.append(_replace_dialogue(content, seg.target) + term)
            else:
                out.append(original)
        result = "".join(out)
        if self._bom_by_file.get(file_name):
            result = "\ufeff" + result
        return result

    # -- internals ----------------------------------------------------------

    def _parse_lines(self, lines: list[str]) -> list[Segment]:
        segments: list[Segment] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if _STRINGS_RE.match(line):
                seg, i = self._handle_strings(lines, i, segments)
            elif (m := _TRANSLATE_RE.match(line)) is not None:
                label = m.group("label")
                if label == "strings":
                    seg, i = self._handle_strings(lines, i, segments)
                elif label in ("old", "new"):
                    seg, i = self._handle_legacy_old_new(lines, i, segments)
                else:
                    seg, i = self._handle_translate(lines, i, segments)
            elif (bm := _BLOCK_HEADER_RE.match(line)) is not None:
                # generic defining block (label/screen/init python/transform/
                # style/...); not translatable -> preserve verbatim + skip (§14)
                i = self._handle_skipped(lines, i, segments)
            else:
                i += 1
        return segments

    def _block_indent(self, lines: list[str], start: int) -> int:
        return len(lines[start]) - len(lines[start].lstrip())

    def _handle_translate(
        self, lines: list[str], start: int, segments: list[Segment]
    ) -> tuple[None, int]:
        indent = self._block_indent(lines, start)
        feature = lines[start].strip()
        i = start + 1
        n = len(lines)
        pending_src_comment: str | None = None  # 最近一条 `# "原文"` 注释行
        while i < n:
            cur = lines[i]
            if not cur.strip():
                i += 1
                continue
            cur_indent = len(cur) - len(cur.lstrip())
            if cur_indent <= indent:
                break  # sibling header closes the block
            if cur.lstrip().startswith("#"):
                # 官方翻译模板（Ren'Py 生成）形态：台词块内 `# "英文原文"`
                # 或 `# role "英文原文"` 注释行。记录之，供台词行作原文
                # 来源（对齐 2.0「原文注释」列语义）；非引号注释（定位行
                # `# game/...` 等）不影响。
                # BUGFIX(role-comment)：role 前缀（ter/kar 等无引号单词）
                # 原正则不匹配 → 该块丢原文来源，中文台词被误判为
                # unrpyc 已译形态（用户实测 2026-10-21：第 3 块起全部中招）。
                cm = re.match(
                    r'^#\s*(?:[\w.]+\s+)?(["\'])(.*)\1\s*$', cur.lstrip())
                pending_src_comment = cm.group(2) if cm else None
                i += 1
                continue
            lm = _LINK_RE.match(cur)
            if lm is not None:
                # `old "X"` 携带原文；紧跟的 `new "Y"` 是已有译文（成对导入，
                # 见 BUGFIX(old-new-pair)）。`link` 是引用，跳过。
                if lm.group("kind") == "old":
                    new_text, new_idx = self._peek_new_pair(lines, i, indent)
                    if new_idx is not None:
                        self._emit_old_new_pair(
                            segments, i, new_idx, source=lm.group("text"),
                            new_text=new_text, feature=feature,
                        )
                        i = new_idx + 1
                        continue
                    self._emit(
                        segments, i, source=lm.group("text"), feature=feature,
                        kind="dialogue", comment=f'# "{lm.group("text")}"',
                        role="",
                    )
                i += 1
                continue
            dlg = _extract_dialogue(cur)
            if dlg is None:
                pending_src_comment = None
                i += 1  # non-dialogue statement; keep verbatim
                continue
            role, _quote, text, _suffix = dlg
            self._emit_dialogue(
                segments, i, source=text, feature=feature, role=role,
                src_comment=pending_src_comment,
            )
            pending_src_comment = None
            i += 1
        return None, i

    def _emit_dialogue(
        self,
        segments: list[Segment],
        idx: int,
        source: str,
        feature: str,
        role: str,
        src_comment: str | None,
    ) -> None:
        """dialogue 台词行立段（BUGFIX(unrpyc-translated)）。

        三种形态（对齐 2.0「原文注释」列语义——原文必须来自可靠来源）：

        1. 官方模板：块内有 `# "英文"` 注释行 → 注释为原文 source，
           台词行内容为译文 → target=台词。
           BUGFIX(sdk-template-pending)：台词与注释原文**相同** → 尚未
           翻译（SDK 刚生成的空模板台词=原文），保持 PENDING；不同
           才是已译（TRANSLATED）。空台词 → PENDING。
           （2.0 rpybak 形态，`#` 行永远存在。）
        2. unrpyc 已译：无注释行且台词含 CJK → 台词即译文，
           source=target=台词，TRANSLATED（不入待翻队列，避免中文→中文）；
        3. unrpyc 未译：无注释行且台词无 CJK → PENDING 正常送翻。
        """
        text = source.strip()
        if src_comment is not None:
            # BUGFIX(sdk-template-pending)：台词与原文相同 = SDK 空模板
            # （原文复读），不是已译；只有台词与原文不同才算 TRANSLATED。
            translated = bool(text) and text != src_comment.strip()
            self._emit(
                segments, idx, source=src_comment, feature=feature,
                kind="dialogue", comment=f'# "{src_comment}"', role=role,
                status=(SegmentStatus.TRANSLATED if translated
                        else SegmentStatus.PENDING),
                target=(text if translated else None),
            )
        elif _has_cjk(text):
            self._emit(
                segments, idx, source=text, feature=feature,
                kind="dialogue", comment=f'# "{text}"', role=role,
                status=SegmentStatus.TRANSLATED, target=text,
            )
        else:
            self._emit(
                segments, idx, source=text, feature=feature,
                kind="dialogue", comment=f'# "{text}"', role=role,
            )

    def _handle_strings(
        self, lines: list[str], start: int, segments: list[Segment]
    ) -> tuple[None, int]:
        indent = self._block_indent(lines, start)
        feature = lines[start].strip()
        i = start + 1
        n = len(lines)
        while i < n:
            cur = lines[i]
            if not cur.strip():
                i += 1
                continue
            cur_indent = len(cur) - len(cur.lstrip())
            if cur_indent <= indent:
                break
            if cur.lstrip().startswith("#"):
                i += 1
                continue
            if _OLD_NEW_HEADER_RE.match(cur):
                # legacy `old:` / `new:` sub-block with string bodies
                i = self._handle_legacy_old_new_body(
                    lines, i, segments, feature, base_indent=cur_indent
                )
                continue
            pm = _OLD_NEW_PAIR_RE.match(cur)
            if pm is not None:
                # BUGFIX(old-new-pair): `old "X"` / `new "Y"` 是一对——old 是
                # 原文，new 是已有译文。此前把两行各自立段，导致中文译文被
                # 当作新的待翻原文（用户实测 2026-10：uid2 = "奥斯瓦尔德的剑"
                # 被送去再翻译）。此处成对消费：old 立段，new 作为 target；
                # new 为空 → PENDING；new 非空 → TRANSLATED。
                new_text, new_idx = self._peek_new_pair(lines, i, indent)
                if pm.group("kind") == "old":
                    if new_idx is not None:
                        self._emit_old_new_pair(
                            segments, i, new_idx, source=pm.group("text"),
                            new_text=new_text, feature=feature,
                        )
                    else:
                        self._emit(
                            segments, i, source=pm.group("text"), feature=feature,
                            kind="strings", comment=f'# "{pm.group("text")}"',
                            role="",
                        )
                    i += 1 if new_idx is None else new_idx - i + 1
                    continue
                # BUGFIX(orphan-new)：孤立的 new 行（无 old 配对）→ 补一条
                # 空 old 与它配对（用户裁决 2026-10-20）：立一条段，
                # source=""（占位）、target=Y、TRANSLATED——保持 old/new
                # 配对形态，Y 的文本在 regenerate 时原位写回 new 行，
                # 不进待翻队列（避免中文→中文浪费 token）。
                self._emit_old_new_pair(
                    segments, i, i, source="", new_text=pm.group("text"),
                    feature=feature,
                )
                i += 1
                continue
            if _is_string_body(cur):
                text = cur.strip()[1:-1]
                self._emit(
                    segments, i, source=text, feature=feature, kind="strings",
                    comment=f'# "{text}"', role="",
                )
                i += 1
                continue
            i += 1  # unknown nested line; keep verbatim
        return None, i

    def _handle_legacy_old_new(
        self, lines: list[str], start: int, segments: list[Segment]
    ) -> tuple[None, int]:
        indent = self._block_indent(lines, start)
        feature = lines[start].strip()
        self._handle_legacy_old_new_body(
            lines, start, segments, feature, base_indent=indent
        )
        # the block ends where indent drops back to `indent`
        i = start + 1
        n = len(lines)
        while i < n:
            cur = lines[i]
            if not cur.strip():
                i += 1
                continue
            if len(cur) - len(cur.lstrip()) <= indent:
                break
            i += 1
        return None, i

    def _peek_new_pair(
        self, lines: list[str], old_idx: int, old_indent: int
    ) -> tuple[str, int | None]:
        """在 `old "X"` 行之后找同级的 `new "Y"` 行（BUGFIX(old-new-pair)）。

        允许中间隔空行/注释；遇到缩进 ≤ old 缩进的非空行视为配对结束。
        返回 `(译文, new 行号)`；找不到返回 `("", None)`。
        """
        i = old_idx + 1
        n = len(lines)
        while i < n:
            cur = lines[i]
            if not cur.strip() or cur.lstrip().startswith("#"):
                i += 1
                continue
            if (len(cur) - len(cur.lstrip())) <= old_indent:
                break
            m = _OLD_NEW_PAIR_RE.match(cur)
            if m is not None and m.group("kind") == "new":
                return m.group("text"), i
            break
        return "", None

    def _emit_old_new_pair(
        self,
        segments: list[Segment],
        old_idx: int,
        new_idx: int,
        source: str,
        new_text: str,
        feature: str,
    ) -> None:
        """`old/new` 一对只立一条段；locator 指向 new 行（回写替换目标）。

        BUGFIX(sdk-template-pending)：new 与 old 原文**相同** → SDK 空模板
        的未翻译占位（原文复读），不是已译 → PENDING；
        new 不同且非空 → TRANSLATED（已带人工/反编译译文，不再机翻）；
        new 为空   → PENDING（正常待译）。
        """
        target = new_text.strip()
        translated = bool(target) and target != source.strip()
        self._emit(
            segments, new_idx, source=source, feature=feature, kind="strings",
            comment=f'# "{source}"', role="",
            status=SegmentStatus.TRANSLATED if translated else SegmentStatus.PENDING,
            target=target if translated else None,
        )

    def _handle_legacy_old_new_body(
        self,
        lines: list[str],
        start: int,
        segments: list[Segment],
        feature: str,
        base_indent: int,
    ) -> int:
        i = start + 1
        n = len(lines)
        while i < n:
            cur = lines[i]
            if not cur.strip():
                i += 1
                continue
            if len(cur) - len(cur.lstrip()) <= base_indent:
                break
            if _is_string_body(cur):
                text = cur.strip()[1:-1]
                # BUGFIX(old-new-pair)：legacy `old:`/`new:` 子块——`old:` 块
                # 的字符串体是原文，其后紧跟的 `new:` 块体是已有译文；成对
                # 消费，避免把译文当原文再立段。此处理解为：字符串体属于
                # `old:` 还是 `new:` 由其头行决定（向上找最近的子块头）。
                if not _body_belongs_to_old(lines, i, base_indent):
                    i += 1  # 孤立 new 体（无 old 配对）不立段，避免误译
                    continue
                new_text, new_idx = "", None
                j = i + 1
                while j < n:
                    nxt = lines[j]
                    if not nxt.strip():
                        j += 1
                        continue
                    if _is_string_body(nxt) and _body_belongs_to_new(lines, j, base_indent):
                        new_text, new_idx = nxt.strip()[1:-1], j
                    break
                if new_idx is not None:
                    target = new_text.strip()
                    self._emit(
                        segments, new_idx, source=text, feature=feature,
                        kind="strings", comment=f'# "{text}"', role="",
                        status=(SegmentStatus.TRANSLATED if target
                                else SegmentStatus.PENDING),
                        target=target or None,
                    )
                    i = new_idx + 1
                    continue
                self._emit(
                    segments, i, source=text, feature=feature, kind="strings",
                    comment=f'# "{text}"', role="",
                )
                i += 1
                continue
            i += 1
        return i

    def _emit(
        self,
        segments: list[Segment],
        idx: int,
        source: str,
        feature: str,
        kind: str,
        comment: str,
        role: str,
        status: SegmentStatus = SegmentStatus.PENDING,
        target: str | None = None,
    ) -> None:
        segments.append(
            Segment(
                uid=len(segments) + 1,
                file=self._file,
                locator=f"# game/{self._file}:{idx + 1}",
                feature=feature,
                kind=kind,
                comment=comment,
                role=role,
                source=source,
                target=target,
                status=status,
                attempts=0,
                error=None,
                indent=0,
            )
        )

    def _handle_skipped(
        self, lines: list[str], start: int, segments: list[Segment]
    ) -> int:
        # DECISION(§14): 无法归类的块原样保留并计入 skipped。整块字节不被改动，
        # 仅记一条 status=SKIPPED 的段用于统计；regenerate 对其逐行输出原文。
        indent = self._block_indent(lines, start)
        self._emit(
            segments, start,
            source=lines[start].rstrip("\r\n"),
            feature=lines[start].strip(),
            kind="skipped",
            comment="",
            role="",
            status=SegmentStatus.SKIPPED,
        )
        i = start + 1
        n = len(lines)
        while i < n:
            cur = lines[i]
            if not cur.strip():
                i += 1
                continue
            if len(cur) - len(cur.lstrip()) <= indent:
                break
            i += 1  # body lines kept verbatim, not walked
        return i


def parse_file(path: Path, game_relpath: str = "") -> list[Segment]:
    """Module-level convenience wrapper around :class:`RenpyParser`."""
    return RenpyParser().parse_file(path, game_relpath)


__all__ = ["RenpyParser", "parse_file"]
