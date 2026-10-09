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
        while i < n:
            cur = lines[i]
            if not cur.strip():
                i += 1
                continue
            cur_indent = len(cur) - len(cur.lstrip())
            if cur_indent <= indent:
                break  # sibling header closes the block
            if cur.lstrip().startswith("#"):
                i += 1  # comment line, kept verbatim
                continue
            lm = _LINK_RE.match(cur)
            if lm is not None:
                # `old "..."` carries the source string; `link` is a reference.
                if lm.group("kind") == "old":
                    self._emit(
                        segments, i, source=lm.group("text"), feature=feature,
                        kind="dialogue", comment=f'# "{lm.group("text")}"',
                        role="",
                    )
                i += 1
                continue
            dlg = _extract_dialogue(cur)
            if dlg is None:
                i += 1  # non-dialogue statement; keep verbatim
                continue
            role, _quote, text, _suffix = dlg
            self._emit(
                segments, i, source=text, feature=feature, kind="dialogue",
                comment=f'# "{text}"', role=role,
            )
            i += 1
        return None, i

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
                text = pm.group("text")
                self._emit(
                    segments, i, source=text, feature=feature, kind="strings",
                    comment=f'# "{text}"', role="",
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
                target=None,
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
