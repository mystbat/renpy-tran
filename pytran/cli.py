"""命令行壳（W5 交付物，设计 §8.3 / §13 / §15）。

子命令：
* ``translate <文件...>`` —— 纯文件模式（M1）：解析 rpy → 翻译 → 写 output/。
* ``game <游戏目录> [--level 0|1|2] [--yes] [--budget-max N]`` —— 游戏目录一键流程（M3，§8.3）。
* ``auto <游戏目录> [--level 0|1|2] [--yes] [--budget-max N]`` —— 全流程自动化（§15）。
* ``auto-batch <父目录>`` —— 批处理（§15.4）。
* ``migrate <旧2.0目录>`` —— 2.0 旧工程迁移导入（§10，W6）。
* ``export-csv`` / ``import-csv`` —— 工程与 CSV 表格互转（§6.4 人工通道）。
* ``ui`` —— 启动图形审校界面（§9）。

自动化分级（§15.1）：
* L2 全自动**必须显式 ``--yes``**，否则启动被拒（§14：写回破坏游戏目录的防守）；
* 启动前 dry-run 成本估算（§15.3），超过 ``--budget-max`` 直接拒绝启动；
* 全部副作用（翻译/解包/写回）通过注入的 engine/unren/exporter 完成，本模块
  不持有具体实现，便于单测用替身。

所有子命令返回进程退出码（0=成功，非 0=失败）。``main`` 入口供 ``python -m pytran.cli``。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pytran.config import Config, TermProfile
from pytran.interfaces import Project, Provider, WriteJournal
from pytran.models import GameFile, Segment, SegmentStatus
from pytran.parser import RenpyParser
from pytran.project import JsonProjectStore, build_project
from pytran.migrate import ProjectMigrator

logger = logging.getLogger(__name__)

# §15.3 成本估算启发式系数（字符→token，含提示词与译文余量）。
_TOKEN_PER_CHAR = 1.3


# --------------------------------------------------------------------------- #
# 策略与预算门（§15.1 / §15.3）—— 纯函数，单测友好
# --------------------------------------------------------------------------- #
def check_level_policy(level: int, yes: bool) -> None:
    """§15.1：L2 必须显式 ``--yes``，否则拒绝启动。

    抛出 ``SystemExit(2)`` 以便 argparse 流程直接以非零码退出。
    """
    if level == 2 and not yes:
        raise SystemExit(
            2, "L2 全自动必须显式传入 --yes 授权破坏性写回（见设计 §15.1 / §14）"
        )


def estimate_token_budget(project: Project, config: Config) -> int:
    """§15.3 dry-run 成本估算：段数×平均长度 → 估算 token。

    采用 ``(Σ原文长度 + Σ已有译文长度) × 系数``，作为保守上界。
    """
    total_chars = 0
    for seg in project.segments:
        total_chars += len(seg.source or "")
        total_chars += len(seg.target or "")
    # 提示词模板与上下文余量：每批固定开销近似为段数×40 字符
    total_chars += len(project.segments) * 40
    return int(total_chars * _TOKEN_PER_CHAR)


def within_budget(estimate: int, budget_max: Optional[int]) -> bool:
    """``--budget-max`` 未给 → 永远通过；否则 estimate 必须 ≤ 上限。"""
    if budget_max is None:
        return True
    return estimate <= int(budget_max)


# --------------------------------------------------------------------------- #
# 注入容器：把具体实现与 CLI 解耦
# --------------------------------------------------------------------------- #
@dataclass
class CliDeps:
    """CLI 依赖注入；测试可传替身，生产由 ``build_deps`` 组装。"""

    config: Config
    provider: Provider
    engine: object = None          # TranslationEngine 实例
    exporter: object = None        # ExporterImpl 实例
    unren: object = None           # UnrenImpl 实例
    store: JsonProjectStore = field(default_factory=JsonProjectStore)
    journal: Optional[WriteJournal] = None


# --------------------------------------------------------------------------- #
# 纯文件模式：translate <文件...>
# --------------------------------------------------------------------------- #
def cmd_translate(
    rpy_files: list[Path],
    *,
    config: Config,
    provider: Provider,
    engine: object,
    exporter: object,
    store: JsonProjectStore,
    work_dir: Path,
) -> int:
    """解析 → 翻译 → 导出 output/（§13 M1）。返回退出码。"""
    if not rpy_files:
        logger.error("translate 需要至少一个 .rpy 文件")
        return 2
    parser = RenpyParser()
    segments: list[Segment] = []
    files: list[GameFile] = []
    for path in rpy_files:
        path = Path(path)
        rel = path.name  # 纯文件模式按文件名定位
        segs = parser.parse_file(path, game_relpath=rel)
        segments.extend(segs)
        files.append(GameFile(name=path.name, game_relpath=rel))
    if not segments:
        logger.error("未从输入文件解析出任何可翻译段落")
        return 1
    project = build_project(segments, files=files)
    profile = TermProfile.load(work_dir / config.profile_path)
    engine.run(project, config, profile, only_uids=None)
    store.save(project, work_dir)
    written = exporter.export_rpy_forced(project, work_dir)
    logger.info("translate 完成：%d 段 → 导出 %d 个文件（%s）",
                len(segments), len(written), work_dir / "output")
    return 0


# --------------------------------------------------------------------------- #
# 游戏目录流程：game <dir> / auto <dir>
# --------------------------------------------------------------------------- #
def cmd_game(
    game_root: Path,
    *,
    deps: CliDeps,
    level: int,
    yes: bool,
    budget_max: Optional[int] = None,
    dry_run: bool = False,
    work_dir: Optional[Path] = None,
) -> int:
    """§8.3 一键流程 / §15 auto。返回退出码。"""
    game_root = Path(game_root)
    work_dir = Path(work_dir or (game_root / "pytran_work"))
    work_dir.mkdir(parents=True, exist_ok=True)

    check_level_policy(level, yes)  # §15.1：L2 必须 --yes

    unren = deps.unren
    if not unren.is_renpy_game(game_root):
        logger.error("不是 Ren'Py 游戏目录：%s", game_root)
        return 2

    # §8.3 ① 解包 RPA ② 反编译 rpyc（测试可注入 fake 跳过真实解包）
    unren.extract_rpa(game_root)
    unren.decompile(game_root)
    # ③ 生成 / 定位 tl 骨架
    unren.generate_skeleton(game_root, deps.config.language, deps.config)

    # 解析 game 目录 rpy，组装项目
    parser = RenpyParser()
    segments: list[Segment] = []
    files: list[GameFile] = []
    for rpy in sorted(game_root.glob("game/**/*.rpy")):
        rel = str(rpy.relative_to(game_root))
        segs = parser.parse_file(rpy, game_relpath=rel)
        segments.extend(segs)
        files.append(GameFile(name=rpy.name, game_relpath=rel))
    if not segments:
        logger.error("游戏目录中未解析出可翻译段落")
        return 1
    project = build_project(segments, files=files, game_root=str(game_root))
    profile = TermProfile.load(work_dir / deps.config.profile_path)
    deps.store.save(project, work_dir)

    # §15.3 成本闸：启动前拒绝超预算
    estimate = estimate_token_budget(project, deps.config)
    if not within_budget(estimate, budget_max):
        logger.error(
            "成本估算 %d token 超过 --budget-max=%s，拒绝启动（§15.3）",
            estimate, budget_max,
        )
        return 3

    if dry_run:
        logger.info("dry-run：估算 %d token，%d 段，未执行翻译/写回", estimate, len(segments))
        return 0

    # L2 事务化写回需要 journal
    journal = deps.journal
    deps.engine.run(project, deps.config, profile, only_uids=None)
    deps.store.save(project, work_dir)

    # §15.2 事务化写回（L0/L1 默认不写回游戏，仅落 output/；L2 写回）
    if level == 2:
        written = deps.exporter.write_back_project(project, work_dir, journal)
        logger.info("L2 写回游戏目录完成：%d 个文件", len(written))
    else:
        written = deps.exporter.export_rpy_forced(project, work_dir)
        logger.info("L%d 仅导出 output/（%d 文件），未写回游戏", level, len(written))
    return 0


def cmd_auto_batch(
    parent: Path,
    *,
    deps: CliDeps,
    level: int,
    yes: bool,
    budget_max: Optional[int] = None,
) -> int:
    """§15.4 批处理：逐个游戏目录执行 L2 流程，隔离失败。"""
    parent = Path(parent)
    games = [p for p in sorted(parent.iterdir()) if p.is_dir() and deps.unren.is_renpy_game(p)]
    if not games:
        logger.error("auto-batch：父目录下未发现 Ren'Py 游戏目录：%s", parent)
        return 2
    summary = {"success": 0, "partial": 0, "failed": 0}
    for g in games:  # 失败隔离：单游戏异常不影响后续
        try:
            rc = cmd_game(g, deps=deps, level=level, yes=yes, budget_max=budget_max)
            if rc == 0:
                summary["success"] += 1
            elif rc == 3:
                summary["partial"] += 1  # 预算超限视为部分（未跑）
            else:
                summary["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - 批处理隔离
            logger.warning("游戏 %s 失败已隔离：%s", g, exc)
            summary["failed"] += 1
    logger.info("auto-batch 汇总：成功 %d / 部分 %d / 失败 %d",
                summary["success"], summary["partial"], summary["failed"])
    return 0 if summary["failed"] == 0 else 1


# --------------------------------------------------------------------------- #
# 2.0 旧工程迁移导入（W6，设计 §10）
# --------------------------------------------------------------------------- #
def cmd_migrate(
    old_dir: Path,
    *,
    work_dir: Path,
) -> int:
    """把 2.0 旧翻译产物（含 rpybak/ 与 split_csv/bak/）迁移进 3.0 工程。

    按「块序 + 行序」对齐回填（§10），未对上的可译行置 FAILED 并带原因。
    匹配率低于 95% 时 ``ProjectMigrator`` 已自动拦下并抛 ``ExportBlocked``。
    """
    old_dir = Path(old_dir)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if not old_dir.is_dir():
        logger.error("migrate：旧 2.0 目录不存在：%s", old_dir)
        return 2
    project = ProjectMigrator().migrate(old_dir, work_dir)
    translated = sum(1 for s in project.segments if s.status == SegmentStatus.TRANSLATED)
    pending = sum(1 for s in project.segments if s.status == SegmentStatus.PENDING)
    failed = sum(1 for s in project.segments if s.status == SegmentStatus.FAILED)
    logger.info(
        "migrate 完成：%d 段 → 已译 %d / 待译 %d / 未匹配 %d（%s）",
        len(project.segments), translated, pending, failed, work_dir / "project.json",
    )
    if failed:
        logger.warning("存在 %d 个未匹配行（FAILED），请人工核对后补译", failed)
    return 0


# --------------------------------------------------------------------------- #
# 人工通道（CSV 往返 / 图形审校，§6.4 / §6.5 / §9）
# --------------------------------------------------------------------------- #
def cmd_export_csv(work_dir: Path) -> int:
    """把当前工程导出为 CSV 表格（§6.4），供人工逐条修改。"""
    work_dir = Path(work_dir)
    deps = build_deps(work_dir)
    project = deps.store.load(work_dir)
    if not project.segments:
        logger.error("export-csv：工程为空（未找到 %s）", work_dir / "project.json")
        return 1
    path = deps.exporter.export_csv(project, work_dir)
    logger.info("export-csv 完成：%d 行 → %s", len(project.segments), path)
    return 0


def cmd_import_csv(csv_path: Path, *, work_dir: Path) -> int:
    """把人工修改过的 CSV 按 uid 主键回灌（§6.4 / §6.2 兜底），并落盘工程。"""
    csv_path = Path(csv_path)
    work_dir = Path(work_dir)
    if not csv_path.is_file():
        logger.error("import-csv：CSV 文件不存在：%s", csv_path)
        return 2
    deps = build_deps(work_dir)
    project = deps.store.load(work_dir)
    if not project.segments:
        logger.error("import-csv：工程为空（未找到 %s）", work_dir / "project.json")
        return 1
    profile = TermProfile.load(work_dir / deps.config.profile_path)
    updated, unmatched = deps.exporter.import_csv(project, csv_path, profile)
    deps.store.save(project, work_dir)
    logger.info("import-csv 完成：更新 %d 行 / 未匹配 %d 行（已保存工程）",
                updated, len(unmatched))
    if unmatched:
        logger.warning("存在 %d 个未匹配行（uid 缺失），已跳过", len(unmatched))
    return 0


def cmd_ui(work_dir: Path) -> int:
    """启动图形审校界面（§9）。"""
    work_dir = Path(work_dir)
    deps = build_deps(work_dir)
    project = deps.store.load(work_dir)
    if not project.segments:
        logger.error("ui：工程为空（未找到 %s），请先 translate / game / migrate",
                     work_dir / "project.json")
        return 1
    from pytran.ui import PytranApp

    PytranApp(project, deps=deps, work_dir=work_dir).run()
    return 0


# --------------------------------------------------------------------------- #
# 生产依赖组装 + argparse 入口
# --------------------------------------------------------------------------- #
def build_deps(work_dir: Path, *, provider: Optional[Provider] = None) -> CliDeps:
    """组装真实依赖；``provider`` 可注入（测试用）。"""
    from pytran.engine import TranslationEngine
    from pytran.exporter import ExporterImpl
    from pytran.journal import JsonWriteJournal
    from pytran.providers import OpenAIProvider
    from pytran.unren import UnrenImpl
    from pytran.validators import Validator

    config = Config.load(work_dir)
    if provider is None:
        provider = OpenAIProvider(config)
    exporter = ExporterImpl(RenpyParser())
    return CliDeps(
        config=config,
        provider=provider,
        engine=TranslationEngine(provider, Validator()),
        exporter=exporter,
        unren=UnrenImpl(),
        store=JsonProjectStore(),
        journal=JsonWriteJournal(work_dir),
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pytran", description="Ren'Py 汉化工具 3.0 命令行壳")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("translate", help="纯文件模式：解析 rpy 并翻译（§13 M1）")
    t.add_argument("files", nargs="+", help="一个或多个 .rpy 文件")
    t.add_argument("--work-dir", default=".", help="工作目录（默认当前）")
    t.set_defaults(func=_dispatch_translate)

    g = sub.add_parser("game", help="游戏目录一键流程（§8.3）")
    g.add_argument("game_dir", help="游戏根目录")
    g.add_argument("--level", type=int, choices=[0, 1, 2], default=0)
    g.add_argument("--yes", action="store_true", help="L2 授权破坏性写回")
    g.add_argument("--budget-max", type=int, default=None)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--work-dir", default=None)
    g.set_defaults(func=_dispatch_game)

    a = sub.add_parser("auto", help="全流程自动化（§15）")
    a.add_argument("game_dir", help="游戏根目录")
    a.add_argument("--level", type=int, choices=[0, 1, 2], default=0)
    a.add_argument("--yes", action="store_true")
    a.add_argument("--budget-max", type=int, default=None)
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--work-dir", default=None)
    a.set_defaults(func=_dispatch_game)

    b = sub.add_parser("auto-batch", help="批处理多个游戏（§15.4）")
    b.add_argument("parent_dir", help="含多个游戏目录的父目录")
    b.add_argument("--level", type=int, choices=[0, 1, 2], default=2)
    b.add_argument("--yes", action="store_true")
    b.add_argument("--budget-max", type=int, default=None)
    b.set_defaults(func=_dispatch_batch)

    m = sub.add_parser("migrate", help="2.0 旧工程迁移导入（§10）")
    m.add_argument("old_dir", help="2.0 产物目录（含 rpybak/ 与 split_csv/bak/）")
    m.add_argument("--work-dir", default=".", help="3.0 工程输出目录（默认当前）")
    m.set_defaults(func=_dispatch_migrate)

    e = sub.add_parser("export-csv", help="导出工程为 CSV 表格（§6.4）")
    e.add_argument("--work-dir", default=".", help="工程目录（默认当前）")
    e.set_defaults(func=_dispatch_export_csv)

    i = sub.add_parser("import-csv", help="把人工修改的 CSV 回灌工程（§6.4）")
    i.add_argument("csv_path", help="CSV 文件路径")
    i.add_argument("--work-dir", default=".", help="工程目录（默认当前）")
    i.set_defaults(func=_dispatch_import_csv)

    u = sub.add_parser("ui", help="启动图形审校界面（§9）")
    u.add_argument("--work-dir", default=".", help="工程目录（默认当前）")
    u.set_defaults(func=_dispatch_ui)
    return p


def _dispatch_translate(args, provider=None) -> int:
    deps = build_deps(Path(args.work_dir), provider=provider)
    return cmd_translate(
        [Path(f) for f in args.files],
        config=deps.config, provider=deps.provider, engine=deps.engine,
        exporter=deps.exporter, store=deps.store, work_dir=Path(args.work_dir),
    )


def _dispatch_game(args, provider=None) -> int:
    deps = build_deps(Path(args.work_dir or args.game_dir), provider=provider)
    return cmd_game(
        Path(args.game_dir), deps=deps, level=args.level, yes=args.yes,
        budget_max=args.budget_max, dry_run=args.dry_run, work_dir=args.work_dir,
    )


def _dispatch_batch(args, provider=None) -> int:
    deps = build_deps(Path(args.parent_dir), provider=provider)
    return cmd_auto_batch(
        Path(args.parent_dir), deps=deps, level=args.level, yes=args.yes,
        budget_max=args.budget_max,
    )


def _dispatch_migrate(args, provider=None) -> int:
    return cmd_migrate(Path(args.old_dir), work_dir=Path(args.work_dir))


def _dispatch_export_csv(args, provider=None) -> int:
    return cmd_export_csv(Path(args.work_dir))


def _dispatch_import_csv(args, provider=None) -> int:
    return cmd_import_csv(Path(args.csv_path), work_dir=Path(args.work_dir))


def _dispatch_ui(args, provider=None) -> int:
    return cmd_ui(Path(args.work_dir))


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit as exc:  # check_level_policy 抛出的策略拒绝
        if isinstance(exc.code, tuple):
            code, msg = exc.code
        elif isinstance(exc.code, int):
            code, msg = exc.code, ""
        else:
            code, msg = 2, str(exc.code)
        if msg:
            logger.error(msg)
        return int(code)


if __name__ == "__main__":
    sys.exit(main())
