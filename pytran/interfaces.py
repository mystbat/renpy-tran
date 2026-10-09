"""跨模块接口契约（契约文件，T0 冻结 —— 任何会话不得修改）。

各工作包以 Protocol 为准实现；设计文档章节号在各方法 docstring 标注。
实现方发现契约不足时：不得改此文件，在交付说明中提「契约变更建议」。
"""
from __future__ import annotations

import enum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable

from pytran.models import Project, Segment, SegmentStatus
from pytran.config import Config, TermProfile

# ---------------------------------------------------------------------------
# 通用类型别名
# ---------------------------------------------------------------------------

Uid = int
# 译文产物：uid → 译文；或 KeepDeclared（AI 申报保留，§5.4）
TranslationResult = dict[Uid, "str | KeepDeclared"]
# 事件回调：engine/后台线程 → UI/CLI（§9：UI 主线程消费）
EventCallback = Callable[[str, dict[str, Any]], None]


class KeepDeclared:
    """AI 申报保留的哨兵值（§5.4）。携带模型给出的一句原因。

    引擎收到 KeepDeclared 后把 Segment 置 keep_declared 并保存 reason。
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover
        return f"KeepDeclared(reason={self.reason!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, KeepDeclared) and other.reason == self.reason


class Detection(str, enum.Enum):
    """漏翻检测器四态判定（§5.3，ADR-0001）。纯函数输出。"""

    PASS = "pass"                        # 放行：残余为空，不入重译队列
    UNTRANSLATED = "untranslated"        # target == source 且剔除豁免后仍有实词
    RESIDUAL = "residual"                # 剔除放行项后仍残留英文
    EMPTY = "empty"                      # 空译文


# ---------------------------------------------------------------------------
# 异常层级
# ---------------------------------------------------------------------------

class PytranError(Exception):
    """所有 pytran 异常的基类。"""


class ConfigError(PytranError):
    """配置缺失/无效（如未配置 key 就发起翻译）。"""


class ParseError(PytranError):
    """rpy 解析结构性失败（单块失败不抛——由 parser 记 skipped）。"""


class ProtocolError(PytranError):
    """模型响应完全不可解析（§4.2 三层兜底均失败且无任何可提取行）。"""


class ProviderError(PytranError):
    """API 调用失败（网络/4xx/5xx，重试耗尽后抛出）。"""


class ProjectError(PytranError):
    """project.json 缺失/损坏/版本不兼容。"""


class GameError(PytranError):
    """游戏目录结构不满足（§8.2 is_renpy_game 失败）或解包/写回失败。"""


class ExportBlocked(PytranError):
    """导出熔断触发（§6.3）。携带明细 dict：{文件: [原因…]}。"""

    def __init__(self, message: str, details: dict[str, list[str]] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class Parser(Protocol):
    """rpy ↔ Segment（W1）。设计文档 §3、§6.1。"""

    def parse_file(self, path: Path, game_relpath: str = "") -> list[Segment]:
        """解析单个 rpy → Segment 列表（未入库）。无法归类的块原样保留并计 SKIPPED（§14）。"""
        ...

    def regenerate(self, project: Project, file_name: str) -> str:
        """由内存行结构 + target 重建 rpy 文本；除台词行外逐字节保持原样（§6.1）。"""
        ...


@runtime_checkable
class Validator(Protocol):
    """V1–V6 校验与自动修复（W2）。设计文档 §5、ADR-0001。"""

    def check(
        self,
        source: str,
        target: str,
        profile: TermProfile,
        allow_keep_declare: bool = False,
    ) -> tuple[str, Optional[str]]:
        """对单行译文做检测+校验。

        返回 (detection, repaired_target)：
        - detection ∈ Detection；
        - repaired_target：自动修复后的译文（无修复则等于 target）；
        - allow_keep_declare：重试轮为 True 时，响应中的申报字段才会被解析（§5.4 注入时机）。
        纯函数：不得读取全局配置；豁免依据全部来自 profile 参数。
        """
        ...


@runtime_checkable
class Provider(Protocol):
    """OpenAI 兼容客户端（W2）。设计文档 §4.1、§4.3。"""

    def translate_batch(
        self,
        numbered_lines: list[tuple[Uid, str]],
        profile: TermProfile,
        context: list[tuple[str, str]] = [],
        allow_keep_declare: bool = False,
        use_fallback_model: bool = False,
    ) -> TranslationResult:
        """发送一批「编号|文本」并返回解析结果（§4.1/§4.2）。

        - numbered_lines: [(uid, source_text)]——uid 作为协议中的行号回传；
        - context: 前文参考 [(src, dst)]，最多 2~3 条（§4.1 要点）；
        - use_fallback_model: True 时切换 fallback_model（§5.3 第 2 次定点升级）；
        - 失败行不在此重试：引擎负责轮次与退避；本方法只对网络层做有限重试+超时。
        """
        ...


@runtime_checkable
class Engine(Protocol):
    """翻译编排（W3）。设计文档 §4.3、§5.3、§5.4、ADR-0003。"""

    def run(
        self,
        project: Project,
        config: Config,
        profile: TermProfile,
        on_event: Optional[EventCallback] = None,
        only_uids: Optional[set[Uid]] = None,
    ) -> Project:
        """执行翻译闭环（可重入：status 已是 translated/reviewed 的行跳过）。

        - only_uids: 仅处理指定行（失败行重译 / 单行重译 / 迁移补漏）；
        - on_event: 进度与日志事件（"progress"/"log"/"segment"/"round"）；
        - 每次状态变更原子写 project（经 ProjectStore）；
        - 线程模型：内部并发，对外同步返回；不得调用任何 UI。
        """
        ...


@runtime_checkable
class ProjectStore(Protocol):
    """project.json 存取（W1）。设计文档 §3.2。"""

    def load(self, work_dir: Path) -> Project: ...
    def save(self, project: Project, work_dir: Path) -> None:
        """原子写入：临时文件 + os.replace（§3.2）。"""
        ...


@runtime_checkable
class WriteJournal(Protocol):
    """写回日志（W4/W5 共用）。设计文档 §15.2 事务化写回。"""

    def begin(self, label: str) -> str:
        """开始一个事务，返回 journal id。"""
        ...

    def record(self, journal_id: str, action: str, src: Path, dst: Path, backup: Path | None) -> None:
        """记录一笔变更（move/copy/delete）及其备份位置。"""
        ...

    def commit(self, journal_id: str) -> None: ...
    def rollback(self, journal_id: str) -> None:
        """按已记录变更逆向回滚。"""
        ...


@runtime_checkable
class Unren(Protocol):
    """游戏解包/反编译/补丁/写回（W4）。设计文档 §8。"""

    def is_renpy_game(self, root: Path) -> bool: ...

    def detect_game_python(self, root: Path) -> Optional[Path]:
        """按 lib/py3-* → lib/windows-* → lib/py2-* 顺序探测（§8.2）；无则 None。"""
        ...

    def extract_rpa(self, game_root: Path, remove_rpa: bool = False) -> list[Path]:
        """提取全部 RPA（vendor/rpatool）；返回解出的顶层文件/目录。"""
        ...

    def decompile(self, game_root: Path, try_harder: bool = False) -> list[Path]:
        """反编译 rpyc（vendor/unrpyc，默认 --init-offset）。"""
        ...

    def generate_skeleton(self, game_root: Path, language: str, config: Config) -> list[Path]:
        """tl 骨架生成：SDK 优先（sdk_bridge），失败回退内置近似骨架（§8.5）。"""
        ...

    def apply_patch(self, game_root: Path, patch: str, hotkeys: dict[str, str] = {}) -> Path:
        """写入 unren-*.rpy 补丁（§8.1/§8.4）；已存在时抛 GameError 由上层询问。"""
        ...

    def remove_patches(self, game_root: Path) -> list[Path]:
        """移除全部 unren-*.rpy。"""
        ...

    def write_back(self, project: Project, work_dir: Path, journal: WriteJournal) -> list[Path]:
        """事务化写回（§8.3、§15.2）：备份→manifest→移入→journal；同时留 output/ 副本。"""
        ...


@runtime_checkable
class SdkBridge(Protocol):
    """Ren'Py SDK tl 骨架桥（W4）。设计文档 §8.5。"""

    def probe(self, sdk_dir: Path, game_root: Path) -> Optional[str]:
        """校验 SDK 与游戏引擎版本匹配（lib/py2-→7.x, py3-→8.x）；返回版本或 None。"""
        ...

    def generate(self, sdk_dir: Path, game_root: Path, language: str, work_dir: Path) -> list[Path]:
        """桥接脚本方式生成 tl 骨架；临时脚本写入→执行→删除，记 journal。"""
        ...


@runtime_checkable
class Exporter(Protocol):
    """导出与 CSV 人工通道（W5）。设计文档 §6.3、§6.4、§6.5。"""

    def export_rpy(self, project: Project, work_dir: Path) -> list[Path]:
        """导出前执行 §6.3 熔断检查，未通过抛 ExportBlocked（含明细）。"""
        ...

    def export_csv(self, project: Project, work_dir: Path) -> Path:
        """六列 CSV（uid|定位符|特征码|角色名|原文|译文）供人工编辑。"""
        ...

    def import_csv(self, project: Project, csv_path: Path, profile: TermProfile) -> tuple[int, int]:
        """CSV 回填：uid 主键 + §6.2 兜底链；返回 (更新行数, 未匹配行数)。"""
        ...

    def bulk_accept(self, project: Project, uids: Iterable[Uid], journal: WriteJournal) -> int:
        """批量全部接受（§6.5）：置 reviewed；受影响行原状态快照入 journal 可撤销。"""
        ...


@runtime_checkable
class Migrator(Protocol):
    """2.0 项目迁移导入（W6）。设计文档 §10。"""

    def migrate(self, old_dir: Path, work_dir: Path) -> Project:
        """按块序+行序对齐回填（不走特征码主键）；未匹配行置 failed 带原因。"""
        ...


__all__ = [
    "Uid", "TranslationResult", "EventCallback", "KeepDeclared", "Detection",
    "PytranError", "ConfigError", "ParseError", "ProtocolError", "ProviderError",
    "ProjectError", "GameError", "ExportBlocked",
    "Parser", "Validator", "Provider", "Engine", "ProjectStore", "WriteJournal",
    "Unren", "SdkBridge", "Exporter", "Migrator",
    "Project", "Segment", "SegmentStatus", "Config", "TermProfile",
]
