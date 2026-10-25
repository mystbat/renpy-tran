"""全文复核（review）：把「原文 / 译文」逐行发给模型，让它逐行判 OK / NG。

设计依据（用户 2026-10-24 决策，取代原 V7 拟声词/编号错配启发式）：

1. **批次内错配无法 100% 判出**——同译文多原文、长度倒挂、尖叫行交叉这些
   指纹都只是「推断」，既有漏报也会误报，用户明确不接受。
2. 改为把**全文**逐行对照交给模型复核：输入成本 ≈ 原文+译文（复核窗口大小
   几乎不影响输入 token），比逐行单发便宜一个数量级（1.06× vs 5.6×）。
3. 复核输出**每行必须有结论**：``uid|ok`` 或 ``uid|NG|<原因>``。
   「模型什么都不报」是本设计最大的风险，所以逐行回执是硬要求；
   没有回执的行一律算「未确认」，由调用方决定重发或重译。
4. 窗口行数 / 复核模型 / 输出上限都可在设置里调（``[Review]`` 段）。
   输出上限才是真瓶颈：2000 行 × 2.5 token ≈ 5000 token，一旦被截断，
   尾部行全部没有回执——因此本模块带**窗口减半重发**（见 ``ReviewRunner``）。

本模块不依赖 Qt、不碰文件系统，只做「造提示词 → 解析回执 → 编排窗口」，
方便单测；线程/UI 由 ``ui.py`` 负责。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "REVIEW_SYSTEM_PROMPT",
    "REVIEW_TAG",
    "ReviewRow",
    "ReviewIssue",
    "ReviewResult",
    "ReviewRunner",
    "build_review_prompt",
    "build_rows",
    "chunk_rows",
    "parse_review_response",
]

# 审校面板里 AI 结论的分类名（与规则检查的分类并列，共用同一张表/重译）
REVIEW_TAG = "AI 校对"

REVIEW_SYSTEM_PROMPT = (
    "你是游戏本地化译文的逐行校对员。用户会给你若干行「编号 / 原文 / 译文」"
    "（制表符分隔）。你的任务是逐行判断译文是否忠实地对应它自己的原文。\n"
    "只输出结论行，不要解释、不要总结、不要 Markdown。"
)

_PROMPT_HEADER = (
    "下面是一部游戏里的一段文本，每行格式为：编号<TAB>原文<TAB>译文。\n"
    "请逐行判断译文与它对应的原文是否一致。\n"
    "\n"
    "判为 ok 的情况：意思一致（允许意译、润色、语序调整、语气词增删）。\n"
    "判为 NG 的情况：\n"
    "  1) 译文明显是别的行的内容（错位/串行）；\n"
    "  2) 漏译（原文的主要内容在译文里没有）；\n"
    "  3) 意思与原文明显不符，或译文是无关内容；\n"
    "  4) 译文残留大量未翻译的原文。\n"
    "\n"
    "输出格式：每个编号输出一行，二选一——\n"
    "  编号|ok\n"
    "  编号|NG|一句中文说明（不超过 30 字）\n"
    "必须为下面出现的**每一个编号**都输出一行结论，不得省略、不得合并、"
    "不得输出其它任何内容。\n"
    "\n"
    "----- 待校对内容开始 -----\n"
)

_PROMPT_FOOTER = "----- 待校对内容结束 -----\n"

# 回执解析：``123|ok`` / ``123|NG|原因``（容忍全角竖线与冒号）
_VERDICT_RE = re.compile(
    r"^(\d+)\s*[|:]\s*([A-Za-z\u4e00-\u9fff]+)\s*(?:[|:]\s*(.*))?$"
)
_OK_WORDS = {"ok", "okay", "pass", "good", "正常", "通过", "是", "好"}
_NG_WORDS = {"ng", "bad", "fail", "no", "错误", "否", "不通过", "异常", "不符"}


@dataclass
class ReviewRow:
    """一行待复核内容（只带复核需要的字段，不引用 models.Segment）。"""

    uid: int
    source: str
    target: str
    file: str = ""
    locator: str = ""
    status: str = ""


@dataclass
class ReviewIssue:
    """模型判为 NG 的一行。"""

    uid: int
    reason: str


@dataclass
class ReviewResult:
    """一次复核的总结果。"""

    issues: list[ReviewIssue] = field(default_factory=list)
    unconfirmed: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    windows: int = 0
    rows: int = 0

    def issue_map(self) -> dict[int, str]:
        return {i.uid: i.reason for i in self.issues}


# ---------------------------------------------------------------------------
# 构造输入
# ---------------------------------------------------------------------------
def _flat(text: str) -> str:
    """把字段压成单行（提示词用 TAB 分列，字段内不能有 TAB/换行）。"""
    return (str(text or "")
            .replace("\r\n", "\\n").replace("\n", "\\n")
            .replace("\r", "\\n").replace("\t", " "))


def build_rows(segments: Iterable[Any]) -> list[ReviewRow]:
    """从段落序列里挑出可复核的行（有译文的），保持原顺序。

    跳过 PENDING 且无译文的段（没内容可校）；其余状态即便译文为空也纳入，
    空译文本身就是模型该报的 NG（漏译）。
    """
    rows: list[ReviewRow] = []
    for s in segments:
        target = (getattr(s, "target", "") or "")
        status = getattr(s, "status", None)
        status_name = getattr(status, "name", str(status or ""))
        if not target.strip() and status_name == "PENDING":
            continue
        rows.append(ReviewRow(
            uid=int(getattr(s, "uid", 0)),
            source=str(getattr(s, "source", "") or ""),
            target=target,
            file=str(getattr(s, "file", "") or ""),
            locator=str(getattr(s, "locator", "") or ""),
            status=status_name,
        ))
    return rows


def chunk_rows(rows: Sequence[ReviewRow], window_lines: int) -> list[list[ReviewRow]]:
    """按窗口行数切块（``window_lines <= 0`` 视为不切）。"""
    size = int(window_lines or 0)
    if size <= 0 or len(rows) <= size:
        return [list(rows)] if rows else []
    return [list(rows[i:i + size]) for i in range(0, len(rows), size)]


def build_review_prompt(rows: Sequence[ReviewRow]) -> str:
    """拼一段复核提示词：表头 + 每行 ``uid\\t原文\\t译文``。"""
    body = "\n".join(
        f"{r.uid}\t{_flat(r.source)}\t{_flat(r.target)}" for r in rows)
    return _PROMPT_HEADER + body + "\n" + _PROMPT_FOOTER


# ---------------------------------------------------------------------------
# 解析回执
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    return (str(text or "")
            .replace("\ufeff", "")
            .replace("｜", "|")
            .replace("：", ":"))


def parse_review_response(
    text: str, uids: Sequence[int] | None = None
) -> dict[int, str | None]:
    """解析模型回执，返回 ``{uid: None(ok) | 原因(NG)}``。

    只保留 ``uids`` 里的编号（``uids=None`` 表示不过滤）。**未出现在返回值
    里的 uid 就是「未确认」**——调用方据此决定重发或重译。
    """
    allowed = set(int(u) for u in uids) if uids is not None else None
    out: dict[int, str | None] = {}
    for raw in _normalize(text).splitlines():
        line = raw.strip().strip("`").strip()
        if not line:
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        m = _VERDICT_RE.match(line)
        if not m:
            continue
        uid = int(m.group(1))
        if allowed is not None and uid not in allowed:
            continue
        word = m.group(2).strip().lower()
        reason = (m.group(3) or "").strip()
        if word in _OK_WORDS:
            out[uid] = None
        elif word in _NG_WORDS:
            out[uid] = reason or "模型判定译文与原文不符"
    return out


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
ReviewCall = Callable[..., str]


class ReviewRunner:
    """把行切成窗口 → 并发请求 → 解析 → 对未确认的行减半重发。

    ``call`` 形如 ``call(prompt=..., system=..., max_tokens=..., ids=[...],
    line_count=N) -> str``；``ui.py`` 里包的是
    ``OpenAIProvider.review_text``。测试可传纯函数替身。

    截断自适应：一个窗口若「未确认行占比 > ``unconfirmed_tolerance``」，
    说明输出被截断或模型偷懒——把该窗口对半切开重发（最多 ``max_rounds``
    轮）。切到单行仍无回执的行进入 ``ReviewResult.unconfirmed``，
    由调用方按「未确认即需重译」处理。
    """

    def __init__(
        self,
        call: ReviewCall,
        *,
        window_lines: int = 2000,
        max_tokens: int = 8192,
        max_workers: int = 3,
        unconfirmed_tolerance: float = 0.2,
        max_rounds: int = 3,
        on_progress: Callable[[dict], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._call = call
        self._window_lines = max(1, int(window_lines or 1))
        self._max_tokens = max(1, int(max_tokens or 1))
        self._max_workers = max(1, int(max_workers or 1))
        self._tolerance = float(unconfirmed_tolerance)
        self._max_rounds = max(1, int(max_rounds or 1))
        self._on_progress = on_progress
        self._should_stop = should_stop

    # -- 公开入口 -----------------------------------------------------------
    def run(self, rows: Sequence[ReviewRow]) -> ReviewResult:
        result = ReviewResult(rows=len(rows))
        rows = list(rows)
        if not rows:
            return result

        windows = chunk_rows(rows, self._window_lines)
        result.windows = len(windows)
        total_windows = len(windows)
        done_windows = 0

        verdicts: dict[int, str | None] = {}
        # 队列元素：(窗口行, 轮次)
        queue: list[tuple[list[ReviewRow], int]] = [(w, 1) for w in windows]

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            pending = {pool.submit(self._run_window, w): (w, rnd)
                       for w, rnd in queue}
            while pending:
                if self._stopped():
                    for fut in pending:
                        fut.cancel()
                    result.errors.append("复核已被用户停止")
                    break
                fut = next(_as_completed(pending))
                window, rnd = pending.pop(fut)
                try:
                    text = fut.result()
                except Exception as exc:  # noqa: BLE001 - 单窗口失败不毁全局
                    msg = (f"窗口 {window[0].uid}..{window[-1].uid} 复核失败："
                           f"{type(exc).__name__}: {exc}")
                    logger.warning(msg)
                    result.errors.append(msg)
                    text = ""
                got = parse_review_response(text, [r.uid for r in window])
                verdicts.update(got)
                done_windows += 1
                self._emit(done_windows, total_windows, len(verdicts), len(rows))

                missing = [r for r in window if r.uid not in got]
                if (missing and len(window) > 1 and rnd < self._max_rounds
                        and len(missing) > len(window) * self._tolerance):
                    # 输出被截断/模型偷懒：减半重发（只重发未确认的部分）
                    for half in chunk_rows(missing,
                                           max(1, len(missing) // 2 + 1)):
                        pending[pool.submit(self._run_window, half)] = (half, rnd + 1)

        for r in rows:
            if r.uid not in verdicts:
                result.unconfirmed.append(r.uid)
            elif verdicts[r.uid] is not None:
                result.issues.append(ReviewIssue(uid=r.uid,
                                                 reason=str(verdicts[r.uid])))
        return result

    # -- 内部 ---------------------------------------------------------------
    def _stopped(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception:  # noqa: BLE001 - 停止钩子自身异常不影响复核
            return False

    def _emit(self, done: int, total: int, confirmed: int, rows: int) -> None:
        if not self._on_progress:
            return
        try:
            self._on_progress({
                "done_windows": done, "total_windows": total,
                "confirmed": confirmed, "rows": rows,
            })
        except Exception:  # noqa: BLE001 - 进度回调异常不影响复核
            logger.debug("review on_progress 回调异常", exc_info=True)

    def _run_window(self, window: Sequence[ReviewRow]) -> str:
        prompt = build_review_prompt(window)
        return self._call(
            prompt=prompt,
            system=REVIEW_SYSTEM_PROMPT,
            max_tokens=self._max_tokens,
            ids=[r.uid for r in window],
            line_count=len(window),
        )


def _as_completed(futures):
    from concurrent.futures import as_completed
    return as_completed(list(futures))
