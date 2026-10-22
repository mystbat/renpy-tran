"""GUI 视图层（基于 PyQt6，重构版本）。

* 延迟 import PyQt6，便于无头环境 import 与测试。
* 视图是 UiState 的薄壳。
* 后台线程发事件到 queue，主线程定时消费。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from pathlib import Path
from typing import Callable, Optional

from pytran.config import Config, TermProfile
from pytran.models import Project
from pytran.project import JsonProjectStore
from pytran.ui_state import REVIEW_FILTERS, UiState

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict], None]

# 审校页筛选项的中文标签。键必须与 UiState.REVIEW_FILTERS 完全一致，
# 选项顺序直接取自 REVIEW_FILTERS（唯一真相源），避免两处清单漂移。
REVIEW_FILTER_LABELS = {
    "all": "全部",
    "pending": "待翻译",
    "translated": "已翻译",
    "failed": "失败",
    "keep_declared": "保留原文",
    "keep_original": "保留原文",
    "reviewed": "已审校",
}

# 审校列表单次渲染的上限：`显示更多` 每次追加这么多条。
# 海量大工程（十万级）一次创建全部 Qt 条目会让窗口假死（实测 13.5 万条约 11 秒）。
REVIEW_PAGE_SIZE = 1000

# 设置对话框中的数值型字段（``_coerce_setting`` 与 ``_apply_settings`` 共用
# 同一份清单）：留空或非法时保留既有值，不会被清空。
# 新增数值字段必须同时登记到这里与 ``_coerce_setting`` 的类型分支，
# 否则字符串会直达 Config 并传给下游（openai SDK 的 timeout /
# Segmenter 的 context_lines 等）。
_NUMERIC_ATTRS = (
    "max_tokens", "max_workers", "round_budget_cap", "max_retry_rounds",
    "context_lines", "block_lines", "keep_declare_min_batch",
    "keep_declare_min_total", "temperature", "top_p", "timeout",
    "keep_declare_rate_limit",
)

# R2：翻译页「最新译文」预览面板单次保留的最大条数。
# 十万级长跑时若无限追加，QListWidget 会重现启动假死问题
# （实测 13.5 万条约 11 秒，见 UI-交付说明 §2）。200 条足够
# 用户判断「译文风格/术语是否如预期」，又不会拖慢渲染。
_PREVIEW_MAX_ROWS = 200


class PytranApp:
    def __init__(
        self,
        project: Project,
        *,
        state: Optional[UiState] = None,
        deps: Optional[object] = None,
        work_dir: Optional[Path] = None,
    ) -> None:
        self.project = project
        self.state = state or UiState(project)
        self.work_dir = Path(work_dir or ".")
        self.deps = deps
        self._events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._window = None
        self._widgets: dict = {}
        self._timer = None
        # 最近一次打开的设置对话框（无头测试驱动用；真实 GUI 不使用）
        self._settings_dialog = None
        # 审校列表当前已渲染的条目上限（随「显示更多」递增）
        self._review_display_cap = REVIEW_PAGE_SIZE
        # 翻译运行态（§9：后台线程跑，UI 反映 busy 态）
        self._translating = False
        # 本次会话的 token 预算上限（§15.3；None = 不限制，不落盘）
        self._budget_max: Optional[int] = None
        # 辅助补丁勾选框（§8.1/§8.4；run() 里填充）
        self._patch_boxes: dict = {}
        # R2 译文预览：是否暂停自动滚动（用户想定住某条细看时用）
        self._preview_paused = False
        # R2 译文预览：本次会话已入列的条数（用于计数标签）
        self._preview_seen = 0
        # R1 试译行数（记住上次选择，避免每次重填）
        self._trial_lines: Optional[int] = None
        # 审校页：译文框是否存在未保存修改
        self._review_dirty = False
        # 导入页「翻译状态」列：本会话内已写回游戏目录的相对路径集合
        # （models.py 冻结不可落盘，故仅内存追踪；写回后置「已翻译」）。
        self._written_back_files: set[str] = set()

    def on_event(self, name: str, data: dict) -> None:
        self._events.put((name, data or {}))

    def _drain_events(self) -> None:
        try:
            while True:
                name, data = self._events.get_nowait()
                self._apply_event(name, data)
        except queue.Empty:
            pass
        finally:
            # BUGFIX(busy-stuck) 最终防线：每轮事件排空后，若不在翻译中
            # 而主进度条仍处于 indeterminate（流动动画），强制恢复确定性。
            # 覆盖一切事件交叠/乱序场景（用户实测：工作完成后绿色流动
            # 永不停止）——终态处理器的收口之外再兜一层。
            if not self._translating:
                pb = self._widgets.get("progress")
                if pb is not None and pb.maximum() == 0 and pb.minimum() == 0:
                    self._set_translate_busy(False)

    def _apply_event(self, name: str, data: dict) -> None:
        if name == "progress":
            self._refresh_progress()
        elif name == "segment":
            # R2：每完成一行就即时入预览面板——用户不必切到审校页
            # 也能看到「已译内容」，确认翻译如预期进行（问题清单 R2）。
            self._on_segment_event(data)
            self._refresh_review()
        elif name == "log":
            self._log(data.get("message", ""), data.get("level", "info"))
        elif name == "round":
            # engine 每轮上报：显示轮次与待译行数。
            # 注意：**不**把进度条打成 busy（setRange(0,0)）——那会让
            # _refresh_progress 的 setValue 永久失效，百分比再也不显示
            # （问题清单 C1）。是否 indeterminate 由 _set_translate_busy 单独控制。
            self._set_translate_busy(True)
            # 细条：每轮以当前队列行数为本次运行总量，开跑时清零。
            self._run_progress_start(data.get("queue_size", 0))
            lbl = self._widgets.get("progress_label")
            if lbl is not None:
                lbl.setText(f"翻译第 {data.get('round', '?')} 轮……"
                            f"（待译 {data.get('queue_size', '?')} 行）")
        elif name == "chunk_done":
            # 9ee271f 新增：块级进度。引擎每完成一个块上报一次。
            # 翻译阶段无逐文件进度，但块数百分比足以反映推进（§9 布局图）。
            self._set_translate_busy(False)
            pb = self._widgets.get("progress")
            done, total = data.get("done", 0), data.get("total", 0) or 0
            if pb is not None and total > 0:
                pb.setValue(int(100.0 * done / total))
            # 细条：按本块行数推进
            self._run_progress_advance(data.get("rows", 0))
            lbl = self._widgets.get("progress_label")
            if lbl is not None:
                extra = f"，本块 {data.get('rows', '?')} 行" if data.get("rows") else ""
                lbl.setText(f"翻译中：块 {done}/{total}{extra}")
        elif name == "refresh_all":
            self._refresh_all()
        elif name == "goto_step":
            # 后台线程请求切页（§9：只有主线程能改 UI 状态）。
            # 翻译完成后由 worker 发出，避免 worker 直接写 state.current_step。
            step = data.get("step", 3)
            if isinstance(step, int) and 0 <= step <= 4:
                self.state.goto(step)
                self._refresh_steps()
        elif name == "translate_finished":
            self._translating = False
            # BUGFIX(busy-stuck)：终态必须恢复确定性模式——round 事件留下的
            # indeterminate 若无 chunk_done/progress 跟进（如停止发生在
            # round 之后、首个 chunk_done 之前），流动动画会永久残留
            # （用户实测 2026-10-21：停止后按钮已灰但进度条仍在流动）。
            self._set_translate_busy(False)
            self._run_progress_reset()
            self._refresh_translate_controls()
        elif name == "translate_stopped":
            # N4：停止终态——让用户明确知道「已停」以及停时的进度快照，
            # 能区分「已排停，正在等当前块跑完」与「按钮没反应」。
            self._translating = False
            # BUGFIX(busy-stuck)：同上，停止终态恢复确定性模式。
            self._set_translate_busy(False)
            self._run_progress_reset()
            self._refresh_translate_controls()
            self._refresh_progress()
            processed = data.get("processed", "?")
            remaining = data.get("remaining", "?")
            lbl = self._widgets.get("progress_label")
            if lbl is not None:
                lbl.setText(f"已停止：本轮已发 {processed} 行，剩余 {remaining} 行未译")
        elif name == "unpack_progress":
            pb = self._widgets.get("unpack_progress")
            lbl = self._widgets.get("unpack_progress_label")
            total = data.get("total") or 0
            if pb is not None:
                if total <= 0:
                    pb.setRange(0, 0)  # busy 指示器（反编译阶段无逐步进度）
                else:
                    pb.setRange(0, 100)
                    pb.setValue(int(100.0 * data.get("done", 0) / total))
            if lbl is not None:
                lbl.setText(data.get("text", ""))

    def _set_translate_busy(self, busy: bool) -> None:
        """切换翻译进度条的 indeterminate 模式。

        ``setRange(0, 0)`` 会让 setValue 永久失效，因此：
        * 需要 indeterminate 时才切到 (0, 0)；
        * 恢复时必须显式 ``setRange(0, 100)``，否则百分比永远回不来（C1）。
        """
        pb = self._widgets.get("progress")
        if pb is None:
            return
        if busy:
            if pb.maximum() != 0 or pb.minimum() != 0:
                pb.setRange(0, 0)
        else:
            if pb.maximum() != 100:
                pb.setRange(0, 100)

    # -- 本次运行进度条（细条，运行时显示） --------------------------------
    def _run_progress_start(self, total: int) -> None:
        """开跑：细条清零并显示。``total`` 为本次运行的待发行数。"""
        rp = self._widgets.get("run_progress")
        if rp is None:
            return
        self._run_total = max(1, int(total or 0))
        self._run_done = 0
        rp.setRange(0, 100)
        rp.setValue(0)
        rp.show()

    def _run_progress_advance(self, rows: int) -> None:
        """每完成一块推进细条。"""
        rp = self._widgets.get("run_progress")
        if rp is None:
            return
        self._run_done = getattr(self, "_run_done", 0) + int(rows or 0)
        total = max(1, int(getattr(self, "_run_total", 1)))
        rp.setValue(min(100, int(100.0 * self._run_done / total)))

    def _run_progress_reset(self) -> None:
        """终态：细条填满后隐藏（给用户一个「跑完了」的视觉确认）。"""
        rp = self._widgets.get("run_progress")
        if rp is None:
            return
        rp.setValue(100)
        rp.hide()
        self._run_total = 0
        self._run_done = 0

    def _ensure_engine_ready(self) -> bool:
        if self.deps is not None and self.deps.engine is not None:
            return True
        try:
            self.deps = self._build_default_deps()
        except Exception as exc:
            self._log(f"无法初始化翻译引擎：{exc}")
            self._log("请在「⚙ 设置」中填写 API Key 后重试")
            return False
        if self.deps.engine is None:
            self._log("尚未配置 API Key：请点「⚙ 设置」填写后保存")
            return False
        return True

    def _translate_in_background(
        self, only_uids: Optional[set] = None, *, auto_goto: bool = True
    ) -> None:
        """后台线程跑翻译。``only_uids`` 非空时只处理这些行（§6.5 单行/失败重译）。

        ``auto_goto=False`` 时完成后**不切页**——用于 R1 试译：
        试译的目的是让人回翻译页看预览面板的结果，不是催他进审校页。

        §9 红线：worker 只通过 ``on_event`` 往队列发事件，
        **不直接改 UI 状态**——切页改为发 ``goto_step`` 事件，由主线程消费。
        """
        if not self._ensure_engine_ready():
            return

        def worker() -> None:
            try:
                deps = self.deps
                deps.engine.run(self.project, deps.config, self._load_profile(),
                                on_event=self.on_event, only_uids=only_uids)
                # 翻译完成 → 切到审校页（由主线程执行，遵守 §9 红线）
                if auto_goto:
                    self.on_event("goto_step", {"step": 3})
                self.on_event("progress", {})
                self.on_event("segment", {})  # Trigger review refresh
            except Exception as exc:
                self.on_event("log", {"message": f"翻译失败：{exc}"})
            finally:
                self.on_event("progress", {})
                self.on_event("translate_finished", {})

        self._translating = True
        self._refresh_translate_controls()
        threading.Thread(target=worker, daemon=True).start()

    def _build_default_deps(self, *, allow_missing_provider: bool = False) -> object:
        """组装生产依赖。

        ``allow_missing_provider=True`` 时缺 API Key 不抛错（GUI 未配置模式）：
        用户在「⚙ 设置」里填好 Key 后，翻译入口会重新组装依赖。
        """
        from pytran.cli import build_deps
        return build_deps(self.work_dir,
                          allow_missing_provider=allow_missing_provider)

    def _current_config(self):
        """取当前 Config；不构造 provider（GUI 未配置模式也不抛）。"""
        if self.deps is not None:
            return self.deps.config
        return self._build_default_deps(allow_missing_provider=True).config

    def _apply_terms(self, terms_map: dict, exempt_add: list[str]) -> None:
        """把编辑后的术语/豁免写回 profile.json（ADR-0004 无 Key 涉及）。"""
        from pytran.terms import rows_to_terms
        profile = self._load_profile()
        # rows_to_terms(rows) -> (terms, auto_exempt)：空译名英文行自动进豁免
        merged, auto_exempt = rows_to_terms(list(terms_map.items()))
        profile.terms = merged
        for ex in list(exempt_add) + auto_exempt:
            if ex not in profile.exempt_english:
                profile.exempt_english.append(ex)
        profile.save(self.work_dir / self.state.project.term_profile)

    def _apply_rules(self, append: str) -> None:
        """写入 prompt_overrides.system_append。"""
        profile = self._load_profile()
        if append:
            profile.prompt_overrides["system_append"] = append
        else:
            profile.prompt_overrides.pop("system_append", None)
        profile.save(self.work_dir / self.state.project.term_profile)

    @staticmethod
    def _coerce_setting(attr: str, raw) -> object:
        """按字段类型收敛对话框字符串；数值留空/非法返回 None（保留既有值）。

        类型表必须与设置对话框 `fields` 清单保持同步（`_open_settings`）：
        整数字段走 int 钳位，小数字段走 float，其余原样返回字符串。
        新增字段时若忘加到这里，字符串会直达 `Config` 并传给下游
        （openai SDK 的 timeout / Segmenter 的 context_lines 等）。
        """
        raw = (str(raw) if raw is not None else "").strip()
        # 整数字段：0/负数会崩线程池或让循环永动，钳到 >=1
        if attr in ("max_tokens", "max_workers", "round_budget_cap",
                    "max_retry_rounds", "context_lines", "block_lines",
                    "keep_declare_min_batch", "keep_declare_min_total"):
            if not raw:
                return None
            try:
                return max(1, int(raw))
            except ValueError:
                return None
        if attr in ("temperature", "top_p", "timeout",
                    "keep_declare_rate_limit"):
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None
        return raw

    def _apply_settings(self, values: dict) -> None:
        """写回 config.ini（§9 L483 字段；ADR-0004：api_key 落盘本机 ini）。

        未配置模式（deps 为 None）下不构造 provider——写配置不需要网络依赖。
        ``_NUMERIC_ATTRS`` 中的字段留空/非法时保留既有值，其余（文本）字段
        允许清空。
        """
        from pytran.cli import build_deps
        cfg = (self.deps.config if self.deps is not None
               else build_deps(self.work_dir, allow_missing_provider=True).config)
        for attr, raw in values.items():
            coerced = self._coerce_setting(attr, raw)
            if coerced is None and attr in _NUMERIC_ATTRS:
                continue  # 数值留空/非法 → 保留既有值
            setattr(cfg, attr, coerced)
        cfg.save(self.work_dir)

    def _validate_sdk(self, sdk_dir: str) -> "tuple[bool, str]":
        """§8.5：SDK 目录校验与引擎版本探测（复用 SdkBridgeImpl.probe）。"""
        if not sdk_dir:
            return (False, "未填写 SDK 目录")
        p = Path(sdk_dir)
        markers = ("renpy.exe", "renpy.sh", "renpy.py")
        if not p.is_dir() or not any((p / m).exists() for m in markers):
            return (False, "目录无效：缺少 renpy 启动器（renpy.exe/sh/py）")
        try:
            from pytran.sdk_bridge import SdkBridgeImpl
            version = SdkBridgeImpl().probe(p, Path(self.project.game_root or self.work_dir))
        except Exception as exc:  # noqa: BLE001
            return (True, f"SDK 有效（版本探测失败：{exc}）")
        if version:
            return (True, f"SDK 有效（引擎版本 {version}）")
        return (True, "SDK 有效（版本未知，按 8.x 处理）")

    def _term_ai_helper(self):
        """术语抽取信号 C 的 AI 助手（W5c §2 信号源 C）。

        返回带 ``complete_text(prompt) -> str`` 的对象，或 ``None``：

        * ``term_extract_use_ai=False`` → None（用户显式关闭）；
        * 无 API Key → None（GUI 未配置模式，**不构造 provider**——
          急切构造 ``OpenAIProvider`` 会抛 ``OpenAIError``）；
        * openai SDK 缺失 / 构造异常 → None 并告警（降级到结构信号）。

        信号 C 是增益不是依赖：返回 None 时 A/D/B 三个结构信号照常工作。
        """
        cfg = self._current_config()
        if not getattr(cfg, "term_extract_use_ai", True):
            return None
        if not getattr(cfg, "api_key", ""):
            return None
        try:
            from pytran.providers import OpenAIProvider
            return OpenAIProvider(cfg)
        except Exception as exc:  # noqa: BLE001 - 无 SDK / 配置异常
            self._log(f"术语 AI 识别不可用（{exc}），仅用结构信号抽取",
                      level="warn")
            return None

    def _load_profile(self) -> TermProfile:
        """加载术语表 profile。

        B8（W5b §5 边界）：profile.json 损坏时按**空 profile** 处理并告警，
        不让 JSON 解析异常一路冒到 GUI 外层把整个界面带崩。
        """
        path = self.work_dir / self.state.project.term_profile
        try:
            return TermProfile.load(path)
        except Exception as exc:  # noqa: BLE001 - 损坏 JSON / 权限等
            self._log(f"profile.json 读取失败（{path}）：{exc}"
                      "——已按空术语表处理，保存后将覆盖原文件", level="warn")
            return TermProfile()

    @staticmethod
    def _icon_path() -> Path:
        """图标文件位置。

        ``pytran/ui.py`` 的上上级目录，在开发态是仓库根；在 PyInstaller
        onedir 打包后是 ``_MEIPASS``（spec 的 datas 把 assets/ 也放在那里）。
        两种情况下 ``assets/pytran.ico`` 都能命中，因此无需区分 frozen。
        """
        return Path(__file__).resolve().parent.parent / "assets" / "pytran.ico"

    def _set_app_icon(self, app) -> None:
        """设置应用图标（对所有窗口生效）。缺失时只记日志，不影响启动——
        图标纯属装饰，不能让它成为 GUI 的启动失败点。"""
        try:
            from PyQt6.QtGui import QIcon
        except Exception:  # noqa: BLE001 - headless 环境可能取不到 QtGui
            return
        path = self._icon_path()
        if not path.is_file():
            logger.warning("图标文件缺失，已跳过：%s", path)
            return
        app.setWindowIcon(QIcon(str(path)))

    def run(self) -> None:  # pragma: no cover
        import sys
        from PyQt6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QPushButton, QProgressBar, QLabel, QListWidget, QTextEdit,
            QSplitter, QStackedWidget, QTreeWidget, QTreeWidgetItem,
            QHeaderView, QComboBox, QGroupBox, QCheckBox
        )
        from PyQt6.QtCore import QTimer, Qt
        
        self._qt = __import__("PyQt6")
        
        app = QApplication.instance()
        if not app:
            app = QApplication(sys.argv)
            
        app.setStyleSheet("""
            QPushButton {
                padding: 6px 16px;
                font-size: 14px;
                min-height: 28px;
            }
        """)
        self._set_app_icon(app)
            
        window = QMainWindow()
        window.setWindowTitle("pytran 3.0 — Ren'Py AI 汉化工具")
        window.resize(1100, 700)
        self._window = window
        
        # UI Structure
        central_widget = QWidget()
        window.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Steps navigation
        step_layout = QHBoxLayout()
        steps = ["选择目录", "导入", "翻译", "审校", "导出写回"]
        self._widgets["steps"] = []
        for i, label in enumerate(steps):
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, idx=i: self._on_step(idx))
            step_layout.addWidget(btn)
            self._widgets["steps"].append(btn)
            
        btn_settings = QPushButton("⚙ 设置")
        btn_settings.clicked.connect(self._open_settings)
        step_layout.addWidget(btn_settings)
        main_layout.addLayout(step_layout)
        
        # Directory Label
        self._widgets["dir_label"] = QLabel()
        self._widgets["dir_label"].setStyleSheet("color: #444; font-size: 13px; font-weight: bold; padding: 4px 0px;")
        self._widgets["dir_label"].setWordWrap(True)
        main_layout.addWidget(self._widgets["dir_label"])
        
        # Stacked Widget for pages (Wizard-like)
        self._widgets["stack"] = QStackedWidget()
        main_layout.addWidget(self._widgets["stack"], stretch=1)
        
        # --- Page 0: Choose Directory ---
        page_choose = QWidget()
        choose_layout = QVBoxLayout(page_choose)
        choose_layout.addWidget(QLabel("第一步：选择需要汉化的游戏目录，或单个 .rpy 脚本文件。"))
        
        choose_btns = QHBoxLayout()
        self._widgets["btn_choose_dir"] = QPushButton("选择游戏目录（推荐）")
        self._widgets["btn_choose_dir"].clicked.connect(self._choose_directory)
        choose_btns.addWidget(self._widgets["btn_choose_dir"])
        
        self._widgets["btn_choose_file"] = QPushButton("选择单个 .rpy 文件")
        self._widgets["btn_choose_file"].clicked.connect(self._choose_file)
        choose_btns.addWidget(self._widgets["btn_choose_file"])
        choose_layout.addLayout(choose_btns)
        
        # 扫描状态提示横幅
        self._widgets["choose_banner"] = QLabel("请先选择游戏根目录或 .rpy 脚本。")
        self._widgets["choose_banner"].setStyleSheet("padding: 6px; font-weight: bold; background-color: #f0f0f0; border-radius: 4px;")
        choose_layout.addWidget(self._widgets["choose_banner"])
        
        # 扫描文件清单树
        self._widgets["scan_tree"] = QTreeWidget()
        self._widgets["scan_tree"].setHeaderLabels(["文件类型", "文件路径 / 名称", "建议操作"])
        self._widgets["scan_tree"].header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        choose_layout.addWidget(self._widgets["scan_tree"], stretch=1)
        
        self._widgets["btn_unpack"] = QPushButton("解包+反编译 (如果是未解包的 rpa/rpyc 游戏，点击此项)")
        self._widgets["btn_unpack"].clicked.connect(self._on_unpack)
        choose_layout.addWidget(self._widgets["btn_unpack"])
        
        # 解包/反编译专用进度条（本页内可见，不复用翻译页的进度条）
        self._widgets["unpack_progress"] = QProgressBar()
        self._widgets["unpack_progress"].setRange(0, 100)
        self._widgets["unpack_progress"].setValue(0)
        choose_layout.addWidget(self._widgets["unpack_progress"])
        self._widgets["unpack_progress_label"] = QLabel("尚未解包。")
        choose_layout.addWidget(self._widgets["unpack_progress_label"])

        # ---- §9 工具区：辅助补丁（仅游戏目录模式可用）----
        # 设计 §8.1/§8.4：四个补丁**修改游戏行为**、与翻译无关，仅调试便利。
        # 默认不勾选、独立勾选区、可一键移除，写入与移除都记日志。
        patch_group = QGroupBox("辅助补丁（修改游戏行为，仅调试用；默认不启用）")
        patch_layout = QVBoxLayout(patch_group)
        patch_hint = QLabel(
            "这些补丁会写 game/unren-*.rpy 改变游戏行为，与翻译无关。\n"
            "勾选后点「注入补丁」；「移除补丁」可随时全部卸载（幂等）。")
        patch_hint.setWordWrap(True)
        patch_hint.setStyleSheet("color: #666; font-size: 12px;")
        patch_layout.addWidget(patch_hint)

        self._patch_boxes = {}
        _PATCH_LABELS = [
            ("console", "☐ 控制台/开发者菜单"),
            ("quick", "☐ 快速存读档 F5/F9"),
            ("skip", "☐ 强制跳过未读文本（TAB/CTRL 快进）"),
            ("rollback", "☐ 强制回滚（滚轮/PAGEUP）"),
        ]
        checks_row = QHBoxLayout()
        for key, label in _PATCH_LABELS:
            cb = QCheckBox(label)
            self._patch_boxes[key] = cb
            checks_row.addWidget(cb)
        checks_row.addStretch(1)
        patch_layout.addLayout(checks_row)

        patch_btns = QHBoxLayout()
        self._widgets["btn_apply_patch"] = QPushButton("注入补丁")
        self._widgets["btn_apply_patch"].clicked.connect(self._on_apply_patches)
        patch_btns.addWidget(self._widgets["btn_apply_patch"])
        self._widgets["btn_remove_patch"] = QPushButton("移除补丁")
        self._widgets["btn_remove_patch"].clicked.connect(self._on_remove_patches)
        patch_btns.addWidget(self._widgets["btn_remove_patch"])
        self._widgets["patch_status"] = QLabel("")
        self._widgets["patch_status"].setStyleSheet("color: #666; font-size: 12px;")
        patch_btns.addWidget(self._widgets["patch_status"])
        patch_btns.addStretch(1)
        patch_layout.addLayout(patch_btns)

        choose_layout.addWidget(patch_group)
        self._widgets["patch_group"] = patch_group
        self._widgets["stack"].addWidget(page_choose)
        
        # --- Page 1: Import ---
        page_import = QWidget()
        import_layout = QVBoxLayout(page_import)
        import_layout.addWidget(QLabel("第二步：扫描并勾选要导入的脚本文件。"))
        
        import_bar = QHBoxLayout()
        self._widgets["btn_select_all"] = QPushButton("全选")
        self._widgets["btn_select_all"].clicked.connect(lambda: self._set_all_import_checks(True))
        import_bar.addWidget(self._widgets["btn_select_all"])
        
        self._widgets["btn_select_none"] = QPushButton("全不选")
        self._widgets["btn_select_none"].clicked.connect(lambda: self._set_all_import_checks(False))
        import_bar.addWidget(self._widgets["btn_select_none"])
        
        import_bar.addStretch()
        
        self._widgets["btn_import_selected"] = QPushButton("导入勾选的脚本")
        self._widgets["btn_import_selected"].clicked.connect(self._import_selected_rpy)
        import_bar.addWidget(self._widgets["btn_import_selected"])
        
        self._widgets["btn_import"] = QPushButton("全部导入")
        self._widgets["btn_import"].clicked.connect(self._import_rpy)
        import_bar.addWidget(self._widgets["btn_import"])

        self._widgets["btn_clear_project"] = QPushButton("清空工程")
        self._widgets["btn_clear_project"].setToolTip(
            "清空已导入的全部段落与文件记录（project.json 归零），回到未导入状态；\n"
            "术语表与游戏目录保留。重新「导入」即按当前解析逻辑重建工程。")
        self._widgets["btn_clear_project"].clicked.connect(self._clear_project)
        import_bar.addWidget(self._widgets["btn_clear_project"])
        import_layout.addLayout(import_bar)
        
        # 待导入脚本树状/勾选表格
        self._widgets["import_tree"] = QTreeWidget()
        self._widgets["import_tree"].setHeaderLabels(["选择", "相对路径", "文件大小", "导入状态", "翻译状态"])
        self._widgets["import_tree"].header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        import_layout.addWidget(self._widgets["import_tree"], stretch=1)
        
        self._widgets["import_summary"] = QLabel("就绪")
        import_layout.addWidget(self._widgets["import_summary"])
        self._widgets["stack"].addWidget(page_import)
        
        # --- Page 2: Translate ---
        page_translate = QWidget()
        translate_layout = QVBoxLayout(page_translate)
        translate_layout.addWidget(QLabel("第三步：自动汉化进程。"))

        # 生效参数 / 成本估算行（§9 L483 + §15.3 成本闸）：
        # 用户改了什么、本次准备烧多少 token，开跑前一眼可见。
        self._widgets["translate_info"] = QLabel("准备就绪")
        self._widgets["translate_info"].setStyleSheet(
            "color: #444; font-size: 12px; padding: 2px 0px;")
        self._widgets["translate_info"].setWordWrap(True)
        translate_layout.addWidget(self._widgets["translate_info"])

        # 预算超限警告（§15.3）：默认隐藏
        self._widgets["budget_warn"] = QLabel("")
        self._widgets["budget_warn"].setStyleSheet(
            "color: #b03030; font-weight: bold; padding: 4px; "
            "background-color: #fdecea; border-radius: 4px;")
        self._widgets["budget_warn"].setWordWrap(True)
        self._widgets["budget_warn"].hide()
        translate_layout.addWidget(self._widgets["budget_warn"])

        progress_layout = QVBoxLayout()
        self._widgets["progress"] = QProgressBar()
        self._widgets["progress"].setRange(0, 100)
        progress_layout.addWidget(self._widgets["progress"])
        # 本次运行进度（较细，仅在翻译运行时显示）：
        # 主条 = 工程累计完成度（done/total，常驻）；细条 = 本次运行已发/待发行数。
        # 每次开跑（round 事件）清零重计，终态（finished/stopped）隐藏。
        self._widgets["run_progress"] = QProgressBar()
        self._widgets["run_progress"].setRange(0, 100)
        self._widgets["run_progress"].setTextVisible(False)
        self._widgets["run_progress"].setFixedHeight(6)
        self._widgets["run_progress"].hide()
        progress_layout.addWidget(self._widgets["run_progress"])
        self._widgets["progress_label"] = QLabel("准备就绪")
        progress_layout.addWidget(self._widgets["progress_label"])
        translate_layout.addLayout(progress_layout)

        translate_btns = QHBoxLayout()
        self._widgets["btn_translate"] = QPushButton("开始自动翻译")
        self._widgets["btn_translate"].clicked.connect(self._start_translate)
        translate_btns.addWidget(self._widgets["btn_translate"])

        # 「重试失败项」与「开始自动翻译」分流（问题清单 B1）：
        # 只重发 FAILED 行，靠 engine.run(only_uids=...) 实现。
        self._widgets["btn_retry"] = QPushButton("重试失败项")
        self._widgets["btn_retry"].clicked.connect(self._retry_failed)
        translate_btns.addWidget(self._widgets["btn_retry"])

        # 停止按钮（9ee271f 的 engine.stop() 协作式中断）：
        # 翻译中可中止，已译结果保留（project.json 原子写，不损坏）。
        self._widgets["btn_stop"] = QPushButton("■ 停止")
        self._widgets["btn_stop"].clicked.connect(self._stop_translate)
        self._widgets["btn_stop"].setEnabled(False)
        translate_btns.addWidget(self._widgets["btn_stop"])

        self._widgets["btn_budget"] = QPushButton("设置预算上限…")
        self._widgets["btn_budget"].clicked.connect(self._set_budget_max)
        translate_btns.addWidget(self._widgets["btn_budget"])

        # R1：「试译」入口——用少量样本验证「模型+提示词+术语表+参数」
        # 这套组合是否合适，再决定是否全量开跑。避免十万段工程跑了几千行
        # 才发现风格不对或术语没生效，返工成本极高（问题清单 R1）。
        self._widgets["btn_trial"] = QPushButton("试译…")
        self._widgets["btn_trial"].clicked.connect(self._trial_translate)
        self._widgets["btn_trial"].setToolTip(
            "只翻译少量样本行，用于确认模型/术语表/参数是否合适。\n"
            "试译走与全量完全相同的校验链，结果写入工程。")
        translate_btns.addWidget(self._widgets["btn_trial"])

        translate_layout.addLayout(translate_btns)

        # R2：「最新译文」预览面板——翻译过程中即时看到已译内容，
        # 确认翻译「如预期进行」（问题清单 R2）。
        # 与审校页的边界：本面板只读预览，不承载编辑；编辑仍去审校页，
        # 避免与审校页的游标/编辑态竞争。
        preview_group = QGroupBox("最新译文（即时预览；修改请到「审校」页）")
        preview_layout = QVBoxLayout(preview_group)
        self._widgets["preview_list"] = QListWidget()
        self._widgets["preview_list"].setToolTip(
            "翻译过程中每完成一行即追加显示；只读，编辑请到审校页。")
        preview_layout.addWidget(self._widgets["preview_list"])
        preview_bar = QHBoxLayout()
        self._widgets["btn_preview_pause"] = QPushButton("暂停滚动")
        self._widgets["btn_preview_pause"].setCheckable(True)
        self._widgets["btn_preview_pause"].toggled.connect(
            self._on_preview_pause_toggled)
        preview_bar.addWidget(self._widgets["btn_preview_pause"])
        self._widgets["preview_count"] = QLabel("")
        self._widgets["preview_count"].setStyleSheet("color: #666; font-size: 12px;")
        preview_bar.addWidget(self._widgets["preview_count"])
        preview_bar.addStretch(1)
        self._widgets["btn_preview_clear"] = QPushButton("清空")
        self._widgets["btn_preview_clear"].clicked.connect(self._clear_preview)
        preview_bar.addWidget(self._widgets["btn_preview_clear"])
        preview_layout.addLayout(preview_bar)
        translate_layout.addWidget(preview_group, stretch=1)
        self._widgets["stack"].addWidget(page_translate)
        
        # --- Page 3: Review ---
        page_review = QWidget()
        review_layout = QVBoxLayout(page_review)
        
        # 审校页工具条：状态筛选 + 条目计数 + 分页显示
        review_bar = QHBoxLayout()
        review_bar.addWidget(QLabel("筛选："))
        self._widgets["review_filter"] = QComboBox()
        # 选项由 UiState.REVIEW_FILTERS 驱动，先填充到位再接信号，
        # 避免构造期 addItem 反复触发刷新。
        for key in REVIEW_FILTERS:
            self._widgets["review_filter"].addItem(REVIEW_FILTER_LABELS[key], key)
        self._widgets["review_filter"].setCurrentIndex(0)
        self._widgets["review_filter"].currentIndexChanged.connect(
            self._on_review_filter_changed)
        review_bar.addWidget(self._widgets["review_filter"])
        
        self._widgets["review_position"] = QLabel("")
        review_bar.addWidget(self._widgets["review_position"])
        review_bar.addStretch()
        
        self._widgets["btn_load_more"] = QPushButton("显示更多")
        self._widgets["btn_load_more"].clicked.connect(self._on_load_more)
        self._widgets["btn_load_more"].setEnabled(False)
        review_bar.addWidget(self._widgets["btn_load_more"])
        review_layout.addLayout(review_bar)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        self._widgets["listbox"] = QListWidget()
        self._widgets["listbox"].currentRowChanged.connect(self._on_list_select)
        splitter.addWidget(self._widgets["listbox"])
        
        # 右侧：上方显示原文全文（只读），下方显示/编辑译文。
        # 左侧列表只截取到 30 字，原文过长时在此处看完整内容。
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        self._widgets["source_view"] = QTextEdit()
        self._widgets["source_view"].setReadOnly(True)
        self._widgets["source_view"].setPlaceholderText(
            "选择左侧条目后，此处显示原文全文")
        self._widgets["source_view"].setStyleSheet(
            "background:#f5f5f5; color:#333; font-size:12px;")
        right_layout.addWidget(self._widgets["source_view"], stretch=1)
        
        self._widgets["editor"] = QTextEdit()
        self._widgets["editor"].textChanged.connect(self._on_editor_changed)
        right_layout.addWidget(self._widgets["editor"], stretch=2)
        
        splitter.addWidget(right_panel)
        splitter.setSizes([300, 800])
        review_layout.addWidget(splitter, stretch=1)
        
        review_btns = QHBoxLayout()
        btn_save = QPushButton("s 保存")
        btn_save.clicked.connect(self._on_save)
        btn_accept = QPushButton("a 通过")
        btn_accept.clicked.connect(self._on_accept)
        btn_flag = QPushButton("f 失败")
        btn_flag.clicked.connect(self._on_flag)
        btn_keep = QPushButton("o 保留原文")
        btn_keep.clicked.connect(self._on_keep)
        review_btns.addWidget(btn_save)
        review_btns.addWidget(btn_accept)
        review_btns.addWidget(btn_flag)
        review_btns.addWidget(btn_keep)
        review_btns.addStretch()
        self._widgets["review_dirty"] = QLabel("")
        self._widgets["review_dirty"].setStyleSheet("color:#b03030; font-size:12px;")
        review_btns.addWidget(self._widgets["review_dirty"])
        review_layout.addLayout(review_btns)
        
        self._widgets["stack"].addWidget(page_review)
        
        # --- Page 4: Export/Writeback ---
        page_export = QWidget()
        export_layout = QVBoxLayout(page_export)
        export_layout.addWidget(QLabel("第四步：写回游戏目录，或导出为 CSV 以供外部校对。"))
        
        self._widgets["btn_export_csv"] = QPushButton("导出 CSV (人工外部审校)")
        self._widgets["btn_export_csv"].clicked.connect(self._on_export_csv)
        export_layout.addWidget(self._widgets["btn_export_csv"])
        
        self._widgets["btn_import_csv"] = QPushButton("从 CSV 导入")
        self._widgets["btn_import_csv"].clicked.connect(self._on_import_csv)
        export_layout.addWidget(self._widgets["btn_import_csv"])
        
        self._widgets["btn_writeback"] = QPushButton("事务化写回游戏目录")
        self._widgets["btn_writeback"].clicked.connect(self._on_writeback)
        export_layout.addWidget(self._widgets["btn_writeback"])
        export_layout.addStretch()
        self._widgets["stack"].addWidget(page_export)
        
        # Log
        self._widgets["log"] = QTextEdit()
        self._widgets["log"].setReadOnly(True)
        self._widgets["log"].setFixedHeight(100)
        main_layout.addWidget(self._widgets["log"])
        
        # Shortcuts — 单字符键仅在审校列表聚焦时生效（WidgetWithChildrenShortcut），
        # 否则会劫持译文编辑框的打字输入。PyQt6 的 QShortcut 构造不支持
        # (key, parent, context) 三参重载 → 用 setContext 属性设置。
        from PyQt6.QtGui import QShortcut, QKeySequence
        from PyQt6.QtCore import Qt
        listbox = self._widgets.get("listbox")
        if listbox is not None:
            listbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut

            def _scoped(key: str, slot) -> None:
                sc = QShortcut(QKeySequence(key), listbox)
                sc.setContext(ctx)
                sc.activated.connect(slot)

            _scoped("A", self._on_accept)
            _scoped("F", self._on_flag)
            _scoped("S", self._on_save)
            _scoped("O", self._on_keep)
            _scoped("J", lambda: self._navigate_list(1))
            _scoped("K", lambda: self._navigate_list(-1))
        sc_save = QShortcut(QKeySequence("Ctrl+S"), window)
        sc_save.activated.connect(self._on_save)

        self._refresh_all()
        self._log("就绪。步骤：选择目录 → 导入 → 翻译 → 审校 → 导出写回；API Key 在「⚙ 设置」中填写")
        
        self._timer = QTimer()
        self._timer.timeout.connect(self._drain_events)
        self._timer.start(100)
        
        window.show()
        app.exec()

    def _on_review_filter_changed(self, _index: int) -> None:
        """切换审校筛选条件：重置游标并回到首页显示量。"""
        combo = self._widgets.get("review_filter")
        if combo is None:
            return
        key = combo.currentData()
        if key:
            self.state.set_filter(str(key))
        self._review_display_cap = REVIEW_PAGE_SIZE
        self._refresh_review()

    def _on_load_more(self) -> None:
        """追加渲染下一批审校条目。"""
        self._review_display_cap += REVIEW_PAGE_SIZE
        self._refresh_review()

    def _displayed_review_rows(self) -> int:
        """当前列表里真实条目（不含尾部提示行）的行数。"""
        lb = self._widgets.get("listbox")
        if lb is None:
            return 0
        n = lb.count()
        from PyQt6.QtCore import Qt
        if n and not (lb.item(n - 1).flags() & Qt.ItemFlag.ItemIsSelectable):
            return n - 1
        return n

    def _navigate_list(self, delta: int) -> None:
        lb = self._widgets.get("listbox")
        if not lb: return
        count = self._displayed_review_rows()
        if count == 0: return
        curr = lb.currentRow()
        next_row = curr + delta
        if 0 <= next_row < count:
            lb.setCurrentRow(next_row)

    def _open_settings(self) -> None:
        from PyQt6.QtWidgets import (QDialog, QFormLayout, QLineEdit, QPushButton,
                                     QVBoxLayout, QMessageBox, QTabWidget, QWidget,
                                     QTableWidget, QTableWidgetItem, QLabel,
                                     QPlainTextEdit, QHBoxLayout, QHeaderView,
                                     QAbstractItemView, QCheckBox)
        from PyQt6.QtCore import Qt

        dlg = QDialog(self._window)
        dlg.setWindowTitle("设置")
        dlg.setModal(True)
        dlg.resize(640, 520)

        # 未配置模式（缺 API Key）也要能打开设置对话框填 Key：
        # 用 allow_missing_provider 避免 OpenAIProvider 构造时抛 OpenAIError。
        cfg = self._current_config()
        profile = self._load_profile()

        tabs = QTabWidget(dlg)

        # ---- Tab 1: 常规（五字段） ----
        page_general = QWidget()
        form = QFormLayout(page_general)
        fields = [
            ("API Key（保存后写入本机 config.ini，不进 git）", "api_key", True),
            ("模型地址 (URL)", "api_url", False),
            ("模型名称", "model", False),
            ("回退模型 (可选，漏翻重试时切换)", "fallback_model", False),
            ("max_tokens（单次请求上限）", "max_tokens", False),
            ("temperature（0~1，越低越稳定）", "temperature", False),
            ("top_p", "top_p", False),
            ("并发数 (max_workers)", "max_workers", False),
            ("请求超时秒 (timeout)", "timeout", False),
            ("重译轮上限 (max_retry_rounds，≤5)", "max_retry_rounds", False),
            ("Ren'Py SDK 目录（可选，生成 tl 骨架）", "renpy_sdk_dir", False),
            ("目标语言", "language", False),
            ("前文参考行数 (context_lines，0=关闭)", "context_lines", False),
            ("切块大小 (block_lines，行/块)", "block_lines", False),
        ]
        entries = {}
        for label, attr, secret in fields:
            e = QLineEdit(str(getattr(cfg, attr, "") or ""))
            if secret:
                e.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(label, e)
            entries[attr] = e
        tabs.addTab(page_general, "常规")

        # ---- Tab 2: 术语表 ----
        page_terms = QWidget()
        v = QVBoxLayout(page_terms)
        btns = QHBoxLayout()
        btn_add = QPushButton("添加行")
        btn_del = QPushButton("删除选中")
        btn_extract = QPushButton("从工程抽取候选…")
        btns.addWidget(btn_add); btns.addWidget(btn_del)
        btns.addWidget(btn_extract); btns.addStretch(1)
        v.addLayout(btns)

        # W5b §5 边界：工程为空时无法抽取，置灰
        btn_extract.setEnabled(bool(self.project.segments))

        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["原文", "译名（留空=按原文保留）"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # §3.2「按原文排序显示」：复用 terms.profile_to_rows 的排序口径
        from pytran.terms import profile_to_rows
        for src, dst in profile_to_rows(profile):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(src))
            table.setItem(row, 1, QTableWidgetItem(dst))
        v.addWidget(table)

        # B4：白名单开关（W5b §3.1 交互设计图中的 ☑ 项）。
        # 勾选时「免翻英文短语」输入框生效；不勾则只用自动推导的豁免。
        exempt_toggle = QCheckBox("命中术语的英文不判为「含英文」（白名单）")
        exempt_toggle.setChecked(True)
        exempt_toggle.setToolTip(
            "勾选后，下方「免翻英文短语」列表参与 §5.1 英文判定；\n"
            "取消勾选则仅使用「译名留空」自动推导出的豁免。")
        v.addWidget(exempt_toggle)

        exempt_label = QLabel(
            "免翻英文短语（每行一个；译名留空的英文条目保存时自动加入）：")
        v.addWidget(exempt_label)
        exempt_edit = QPlainTextEdit("\n".join(profile.exempt_english))
        exempt_edit.setMaximumHeight(80)
        v.addWidget(exempt_edit)

        def add_row():
            table.insertRow(table.rowCount())
            table.editItem(table.item(table.rowCount() - 1, 0))

        def del_rows():
            for it in reversed(sorted(table.selectedIndexes())):
                table.removeRow(it.row())

        def extract_candidates():
            """B3：弹候选对话框，由用户逐条决定译名/忽略（W5b §3.2 / W5c §2）。

            旧实现把候选直接 append 进表格，且第二列写死 "(出现 N 次)"
            这个伪译名——用户若直接保存，该字符串会被当成译名写进 terms。

            W5c：候选来自四个信号源（A 角色定义 / D 对话 role / B old 短串 /
            C AI 识别），弹窗展示「来源」列让用户判断置信度。AI 信号受
            ``term_extract_use_ai`` 开关与 API Key 双重约束，任一缺失时
            A/D/B 仍正常工作。
            """
            from pytran.terms import extract_candidates
            cands = extract_candidates(
                self.project.segments,
                profile,
                game_root=self.project.game_root or None,
                ai_helper=self._term_ai_helper(),
                language=(getattr(self._current_config(), "language", "")
                          or "chinese"),
            )
            if not cands:
                QMessageBox.information(dlg, "抽取", "未发现新的候选术语")
                return
            chosen = self._pick_term_candidates(dlg, cands)
            for term, translation in chosen:
                row = table.rowCount()
                table.insertRow(row)
                it0 = QTableWidgetItem(term)
                it0.setBackground(Qt.GlobalColor.lightGray)  # 新候选待确认
                table.setItem(row, 0, it0)
                table.setItem(row, 1, QTableWidgetItem(translation))

        btn_add.clicked.connect(add_row)
        btn_del.clicked.connect(del_rows)
        btn_extract.clicked.connect(extract_candidates)
        tabs.addTab(page_terms, "术语表")

        # ---- Tab 3: 翻译规则 ----
        page_rules = QWidget()
        rv = QVBoxLayout(page_rules)
        hint = QLabel("追加在系统提示词末尾的自定义规则（风格/人名/格式要求）。\n"
                      "不要修改输出格式规则（JSONL）——那由程序保证。下次翻译生效。")
        hint.setWordWrap(True)
        rv.addWidget(hint)
        rules_edit = QPlainTextEdit(
            profile.prompt_overrides.get("system_append", ""))
        rv.addWidget(rules_edit)
        tabs.addTab(page_rules, "翻译规则")

        layout = QVBoxLayout(dlg)
        layout.addWidget(tabs)

        def save():
            # 常规页字段统一走 _apply_settings：它负责按类型收敛
            # （_coerce_setting）并对数值字段做「留空保留既有值」。
            # 直接 setattr 会把所有字段写成字符串——timeout / context_lines
            # 等会以 str 传给 SDK 与 Segmenter，必须走这条经过测试的路。
            values = {attr: e.text().strip() for attr, e in entries.items()}
            self._apply_settings(values)

            # 术语表：复用 terms.py 的纯逻辑（rows_to_terms 负责「空译名 +
            # 英文 → 自动进豁免」与「重复原文后者覆盖」），GUI 只做收集。
            from pytran.terms import rows_to_terms, detect_duplicate_originals
            rows: list[tuple[str, str]] = []
            for row in range(table.rowCount()):
                it0, it1 = table.item(row, 0), table.item(row, 1)
                if it0 is None:
                    continue
                src = it0.text().strip()
                dst = (it1.text().strip() if it1 else "")
                if not src:
                    continue
                rows.append((src, dst))

            dups = detect_duplicate_originals(rows)
            terms_map, auto_exempt = rows_to_terms(rows)

            exempt_add: list[str] = list(auto_exempt)
            # B4：白名单开关控制「免翻英文短语」输入框是否生效
            if exempt_toggle.isChecked():
                for ex in exempt_edit.toPlainText().splitlines():
                    ex = ex.strip()
                    if ex and ex not in exempt_add:
                        exempt_add.append(ex)
            self._apply_terms(terms_map, exempt_add)

            append = rules_edit.toPlainText().strip()
            self._apply_rules(append)

            # B6：保存结果汇总（W5b §3.2「弹窗汇总保存结果」）
            parts = [
                "已保存到 config.ini / profile.json。",
                f"术语 {len(terms_map)} 条，免翻短语 {len(exempt_add)} 条。",
            ]
            if dups:
                parts.append(f"重复原文已合并（后值覆盖）：{'、'.join(dups[:5])}"
                             + ("…" if len(dups) > 5 else ""))
            parts.append("API Key 写入本机 config.ini（已 gitignore，不进版本库）；"
                         "环境变量 TL_API_KEY 优先级更高。")
            parts.append("术语表与规则将在下次翻译时生效。")
            QMessageBox.information(dlg, "设置", "\n".join(parts))
            dlg.accept()

        btn_save = QPushButton("保存")
        btn_save.clicked.connect(save)
        layout.addWidget(btn_save)

        # 记录最近一次打开的设置对话框，供无头测试驱动（真实 GUI 不受影响）。
        self._settings_dialog = dlg
        dlg.exec()

    def _pick_term_candidates(self, parent, candidates) -> list[tuple[str, str]]:
        """B3：候选术语选择对话框（W5b §3.2 / W5c §3）。

        列：候选词 | 来源 | 出现次数 | 译名。用户逐条填译名或删行忽略；
        确定后只把**填了译名**或**标记保留**的条目并回表格。
        译名留空且原文含英文 → 视为「按原文保留」。

        「来源」列（W5c §3）让用户知道置信度：
        ``A`` = `define x = Character("N")` 权威定义；
        ``D`` = 对话行前缀的说话人名（结构信号）；
        ``B`` = 作者声明的 `old "X"` 短串；
        ``C`` = AI 识别（已按出现次数过滤，仍建议抽查）。

        ``candidates`` 为 :class:`pytran.terms.Candidate` 列表。
        返回 ``[(term, translation)]``；``translation`` 可为空串（保留原文）。
        """
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
            QPushButton, QLabel, QHeaderView, QCheckBox,
        )
        from PyQt6.QtCore import Qt

        dlg = QDialog(parent)
        dlg.setWindowTitle("候选术语")
        dlg.resize(620, 480)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            "下表为从工程中抽取的候选术语。填写译名后点「确定」并入术语表；\n"
            "译名留空 = 按原文保留（原文含英文时自动加入免翻短语）。\n"
            "不想要的条目直接删行或留空忽略。\n"
            "来源：A=角色定义（最可靠） D=对话说话人 B=old短串 C=AI识别（建议抽查）"))

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["候选词", "来源", "出现次数", "译名（留空=保留）"])
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        for c in candidates:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(c.term))
            src = QTableWidgetItem(getattr(c, "source", "") or "")
            src.setFlags(src.flags() & ~Qt.ItemFlag.ItemIsEditable)
            src.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, 1, src)
            cnt = QTableWidgetItem(str(c.count))
            cnt.setFlags(cnt.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 2, cnt)
            table.setItem(row, 3, QTableWidgetItem(""))
        layout.addWidget(table)

        bar = QHBoxLayout()
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(
            lambda: [table.removeRow(it.row())
                     for it in reversed(sorted(table.selectedIndexes()))])
        bar.addWidget(btn_del)
        bar.addStretch(1)
        btn_ok = QPushButton("确定")
        btn_ok.setDefault(True)
        btn_cancel = QPushButton("取消")
        bar.addWidget(btn_ok)
        bar.addWidget(btn_cancel)
        layout.addLayout(bar)

        result: list[tuple[str, str]] = []
        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)

        if dlg.exec():
            for row in range(table.rowCount()):
                it0 = table.item(row, 0)
                if it0 is None:
                    continue
                term = it0.text().strip()
                if not term:
                    continue
                # 译名列已因新增「来源」列右移到 index 3
                it3 = table.item(row, 3)
                translation = (it3.text().strip() if it3 else "")
                # 只并入「填了译名」或「原文含英文（保留原文）」的条目；
                # 纯中文候选留空则视为忽略，避免把无意义条目写进术语表。
                if translation or re.search(r"[A-Za-z]", term):
                    result.append((term, translation))
        return result

    def _on_step(self, idx: int) -> None:
        stack = self._widgets.get("stack")
        if stack:
            stack.setCurrentIndex(idx)
        self._highlight_active_step(idx)
        if idx == 3:
            self._refresh_review()

    def _highlight_active_step(self, active_idx: int) -> None:
        for i, b in enumerate(self._widgets.get("steps", [])):
            if i == active_idx:
                b.setStyleSheet("font-weight: bold; background-color: #2a7ae2; color: white;")
            else:
                b.setStyleSheet("")
            
    def _on_export_csv(self) -> None:
        """导出工程为 CSV 供人工编辑（§6.4）。"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        try:
            if self.deps is None:
                self.deps = self._build_default_deps()
            path, _ = QFileDialog.getSaveFileName(
                self._window, "导出 CSV", str(self.work_dir / "translations.csv"),
                "CSV (*.csv)")
            if not path:
                return
            updated = self.deps.exporter.export_csv(self.project, Path(path))
            self._log(f"已导出 CSV：{path}")
            QMessageBox.information(self._window, "导出", f"已导出 {path}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"导出失败：{exc}", level="warn")
            QMessageBox.warning(self._window, "导出失败", str(exc))

    def _on_import_csv(self) -> None:
        """回灌人工修改后的 CSV（§6.4，按 uid 匹配）。"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        try:
            if self.deps is None:
                self.deps = self._build_default_deps()
            path, _ = QFileDialog.getOpenFileName(
                self._window, "选择要回灌的 CSV", str(self.work_dir),
                "CSV (*.csv);;所有文件 (*.*)")
            if not path:
                return
            profile = self._load_profile()
            updated, unmatched = self.deps.exporter.import_csv(
                self.project, Path(path), profile)
            self.deps.store.save(self.project, self.work_dir)
            self._log(f"CSV 回灌完成：更新 {updated} 行，未匹配 {unmatched} 行")
            self._refresh_review()
            QMessageBox.information(
                self._window, "导入",
                f"更新 {updated} 行，未匹配 {unmatched} 行\n"
                f"（未匹配行已跳过，详见日志）")
        except Exception as exc:  # noqa: BLE001
            self._log(f"CSV 回灌失败：{exc}", level="warn")
            QMessageBox.warning(self._window, "导入失败", str(exc))

    def _choose_file(self) -> None:
        """选择 .rpy 文件（纯文件模式，支持多选）。"""
        from PyQt6.QtWidgets import QFileDialog
        rpys, _ = QFileDialog.getOpenFileNames(
            self._window, "选择 .rpy 文件（纯文件模式，可多选）",
            str(self.work_dir), "Ren'Py 脚本 (*.rpy);;所有文件 (*.*)")
        if rpys:
            self._pending_rpy_list = rpys
            self._pending_rpy = rpys[0]
            self.state.goto(1)
            self._log(f"已选择 {len(rpys)} 个 rpy 文件：")
            for r in rpys[:10]:
                self._log(f"  {r}")
            if len(rpys) > 10:
                self._log(f"  … 共 {len(rpys)} 个")
            self._log("点「导入」开始解析")

    def _choose_directory(self) -> None:
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        
        # 决定文件对话框的初始目录：若已有游戏目录或文件，则从那里开始
        initial_dir = str(self.work_dir)
        if self.project.game_root and Path(self.project.game_root).exists():
            initial_dir = self.project.game_root
        elif getattr(self, "_pending_rpy", None) and Path(self._pending_rpy).parent.exists():
            initial_dir = str(Path(self._pending_rpy).parent)
        
        game_dir = QFileDialog.getExistingDirectory(
            self._window, "选择 Ren'Py 游戏根目录（含 game/ 的目录）", initial_dir)
            
        if game_dir:
            if self.project.segments or self.project.game_root != game_dir:
                # 换了目录：旧工程数据（段列表/导入文件）全部作废，防止
                # 旧游戏的可翻译行混进新游戏的审校列表。
                from pytran.models import Project, ProjectStats
                old_root = self.project.game_root
                self.project = Project(version=3, app="pytran",
                                       game_root=game_dir)
                self.state = type(self.state)(self.project)
                # 上一游戏的术语表/提示词 profile 不可带入新游戏（§7.2）
                try:
                    (self.work_dir / "profile.json").unlink(missing_ok=True)
                    (self.work_dir / "project.json").unlink(missing_ok=True)
                except OSError:
                    pass
                self._log(f"已切换游戏目录（{old_root or '无'} → {game_dir}），"
                          "旧导入数据与术语表已清空")
            self.project.game_root = game_dir
            JsonProjectStore().save(self.project, self.work_dir)
            self.state.goto(1)
            self._refresh_tools()  # 解包/写回按钮的置灰依赖 game_root，选完立即刷新
            # 给用户一个状态判断：有 rpy 直接导入；只有 rpa/rpyc 需要先解包
            game = Path(game_dir) / "game"
            n_rpy = len(list(game.glob("**/*.rpy"))) if game.is_dir() else 0
            n_rpa = len(list(game.glob("*.rpa"))) if game.is_dir() else 0
            n_rpyc = len(list(game.glob("**/*.rpyc"))) if game.is_dir() else 0
            self._log(f"已选择游戏目录：{game_dir}")
            if n_rpy:
                self._log(f"发现 {n_rpy} 个 .rpy → 点「导入」直接解析")
            elif n_rpa or n_rpyc:
                self._log(f"发现 {n_rpa} 个 RPA / {n_rpyc} 个 rpyc（未解包状态）"
                          "→ 点「解包+反编译」后再导入")
            else:
                self._log("game 目录为空或不存在，请确认选的是游戏根目录")
            self._refresh_dir_label()
            return
            
        rpy, _ = QFileDialog.getOpenFileName(
            self._window, "或选择单个 .rpy 文件（纯文件模式）", initial_dir, "Ren'Py 脚本 (*.rpy);;所有文件 (*.*)")
            
        if rpy:
            if self.project.segments:
                from pytran.models import Project, ProjectStats
                self.project = Project(version=3, app="pytran")
                self.state = type(self.state)(self.project)
                self._log("已选择新文件：旧导入数据已清空")
            self._pending_rpy = rpy
            self.state.goto(1)
            self._log(f"已选择 rpy 文件：{rpy}")
            self._log("点「导入」开始解析")

    def _set_all_import_checks(self, checked: bool) -> None:
        tree = self._widgets.get("import_tree")
        if not tree:
            return
        from PyQt6.QtCore import Qt
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _import_selected_rpy(self) -> None:
        tree = self._widgets.get("import_tree")
        if not tree:
            return
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMessageBox
        selected_files: list[Path] = []
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                path_obj = item.data(1, Qt.ItemDataRole.UserRole)
                if path_obj:
                    selected_files.append(Path(path_obj))
        if not selected_files:
            QMessageBox.information(self._window, "导入", "请至少勾选一个需要导入的脚本文件！")
            return
        self._do_import_files(selected_files)

    def _selected_import_files(self) -> list[Path]:
        """从导入页勾选树收集用户勾选的文件。

        N2（问题清单）：旧实现 ``return picked or discovered`` 使
        **全取消勾选 = 导入全部**，与用户直觉相反，也破坏了 `9ee271f`
        第 ⑤ 项「取消勾选即跳过」的部分导入意图。

        现在的语义：
        * 勾选树已构建 → 严格按勾选状态返回（全取消 = 空列表，由调用方提示）；
        * 树尚未构建（`_refresh_import_tree` 未跑过）→ 回退全部发现。
        """
        tree = self._widgets.get("import_tree")
        if tree is None:
            return self._get_discovered_rpy_files()
        if tree.topLevelItemCount() == 0:
            # 树是空的：说明还没刷新过，此时才回退到「全部发现」
            return self._get_discovered_rpy_files()
        from PyQt6.QtCore import Qt
        picked: list[Path] = []
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            if it.checkState(0) == Qt.CheckState.Checked:
                path_obj = it.data(1, Qt.ItemDataRole.UserRole)
                if path_obj:
                    picked.append(Path(path_obj))
        return picked

    def _clear_project(self) -> None:
        """清空工程：段列表/导入文件记录/project.json 归零（用户需求 2026-10-20）。

        保留 game_root / 术语表（同目录重新导入即按当前解析逻辑重建）。
        有未保存的人工译文时弹确认，防误触。
        """
        from PyQt6.QtWidgets import QMessageBox
        if not self.project.segments and not self.project.files:
            QMessageBox.information(self._window, "清空工程", "工程已经是空的。")
            return
        reviewed = sum(
            1 for s in self.project.segments
            if s.status.value in ("translated", "reviewed")
        )
        msg = (f"确定清空工程？\n\n将删除 {len(self.project.segments)} 条段落"
               f"（含 {reviewed} 条已译/已审校）的导入记录，project.json 归零。\n"
               "术语表与游戏目录保留；重新导入即按当前解析逻辑重建。")
        ret = QMessageBox.question(
            self._window, "清空工程", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        from pytran.models import Project
        game_root = self.project.game_root
        self.project = Project(version=3, app="pytran", game_root=game_root)
        self.state = type(self.state)(self.project)
        JsonProjectStore().save(self.project, self.work_dir)
        self._log(f"工程已清空（原 {reviewed} 条已译/已审校记录一并移除）；"
                  "游戏目录与术语表保留，可重新导入")
        self.state.goto(1)
        self._written_back_files = set()
        self._refresh_all()

    def _import_rpy(self) -> None:
        # 按勾选树导入（用户可取消勾选实现部分导入，N2）
        all_files = self._selected_import_files()
        if not all_files:
            from PyQt6.QtWidgets import QMessageBox
            # 区分「没有可导入文件」与「用户取消了全部勾选」两种情形
            if self._get_discovered_rpy_files():
                QMessageBox.information(
                    self._window, "导入",
                    "没有勾选任何脚本。\n"
                    "请至少勾选一个文件，或用「全选」恢复默认选择。")
            else:
                QMessageBox.information(
                    self._window, "导入",
                    "未发现可供导入的 .rpy 文件，请先在「选择目录」中选择游戏或解包。")
            return
        self._do_import_files(all_files)

    def _get_discovered_rpy_files(self) -> list[Path]:
        files: list[Path] = []
        game_root = self.project.game_root
        if game_root:
            tl = Path(game_root) / "game" / "tl"
            pattern = tl if tl.is_dir() else Path(game_root) / "game"
            files = sorted(pattern.glob("**/*.rpy"))
        elif getattr(self, "_pending_rpy_list", None):
            files = [Path(x) for x in self._pending_rpy_list if Path(x).is_file()]
        elif getattr(self, "_pending_rpy", None):
            p = Path(self._pending_rpy)
            if p.is_file():
                files = [p]
        return files

    def _do_import_files(self, target_files: list[Path]) -> None:
        from PyQt6.QtWidgets import QMessageBox
        parser = None
        try:
            from pytran.parser import RenpyParser
            parser = RenpyParser()
        except Exception as exc:
            self._log(f"解析器初始化失败：{exc}")
            return
        segs = []
        files = []
        game_root = self.project.game_root
        for rpy in target_files:
            if game_root:
                try:
                    rel = rpy.relative_to(Path(game_root) / "game").as_posix()
                except ValueError:
                    rel = rpy.name
            else:
                rel = rpy.name
            segs.extend(parser.parse_file(rpy, game_relpath=rel))
            files.append((rel, rpy))

        if not segs:
            self._log("未从选中脚本中解析出有效文本段落。")
            return

        from pytran.models import GameFile
        from pytran.project import build_project
        import hashlib
        gf = []
        for rel, rpy in files:
            gf.append(GameFile(name=rel, sha256=hashlib.sha256(rpy.read_bytes()).hexdigest(), game_relpath=rel))
        self.project = build_project(segs, files=gf, game_root=str(game_root or ""))
        self.state = type(self.state)(self.project)
        self.state.goto(2)
        JsonProjectStore().save(self.project, self.work_dir)
        self._log(f"导入完成：从 {len(files)} 个文件中解析出 {len(segs)} 段（skipped {self.project.stats.skipped}）→ project.json 已保存")
        self._log("已自动激活「翻译」阶段，点击「开始自动翻译」即可启动")
        self._refresh_all()

    def _start_translate(self) -> None:
        if not self._ensure_engine_ready():
            return
        if not self.state.has_segments:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self._window, "翻译", "工程为空：请先完成「选择目录」和「导入」")
            return
        # §15.3 成本闸：超预算时要求确认，不静默开跑
        if not self._confirm_budget():
            return
        self.state.goto(2)
        self._translate_in_background()

    def _retry_failed(self) -> None:
        """§6.5「失败重译」：只重发 FAILED 行（问题清单 B1）。

        与「开始自动翻译」分流：两者都只收 PENDING/FAILED（W5g §2 起，
        KEEP_DECLARED/KEEP_ORIGINAL 不再进队列），前者只针对失败行，
        语义与按钮文案一致。
        """
        if not self._ensure_engine_ready():
            return
        from pytran.models import SegmentStatus
        failed = {s.uid for s in self.project.segments
                  if s.status == SegmentStatus.FAILED}
        if not failed:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self._window, "重试", "当前没有失败项需要重试。")
            return
        if not self._confirm_budget(only_uids=failed):
            return
        self._log(f"重试 {len(failed)} 个失败项……")
        self._translate_in_background(only_uids=failed)

    def _stop_translate(self) -> None:
        """9ee271f：协作式中止。当前 in-flight 的块会跑完，后续块跳过。"""
        engine = getattr(self.deps, "engine", None) if self.deps else None
        if engine is not None and hasattr(engine, "stop"):
            engine.stop()
            self._log("已请求停止翻译（当前块完成后不再发送新块）")
        else:
            self._log("翻译引擎未就绪，无需停止", level="warn")
        lbl = self._widgets.get("progress_label")
        if lbl is not None:
            lbl.setText("正在停止……")

    def _set_budget_max(self) -> None:
        """设置本次会话的 token 预算上限（§15.3，不落盘）。"""
        from PyQt6.QtWidgets import QInputDialog
        cur = "" if self._budget_max is None else str(self._budget_max)
        text, ok = QInputDialog.getText(
            self._window, "预算上限",
            "本次翻译的 token 预算上限（留空 = 不限制）：", text=cur)
        if not ok:
            return
        text = (text or "").strip()
        self._budget_max = int(text) if text.isdigit() and int(text) > 0 else None
        self._refresh_translate_info()

    def _confirm_budget(self, only_uids: Optional[set] = None) -> bool:
        """§15.3 成本闸：估算超预算时询问用户，返回是否继续。"""
        estimate = self._estimate_tokens(only_uids)
        if self._budget_max is None or estimate <= self._budget_max:
            return True
        from PyQt6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self._window, "超出预算",
            f"估算消耗约 {estimate:,} token，超过设定上限 {self._budget_max:,}。\n"
            "仍要开始翻译吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ret == QMessageBox.StandardButton.Yes

    def _translatable_uids(self) -> list:
        """返回本次可译的 uid（按 uid 升序，与切块顺序一致）。

        口径与 ``TranslationEngine._build_queue`` 严格一致：
        PENDING / FAILED。W5g §2：KEEP_DECLARED 与 KEEP_ORIGINAL 均不进
        重译队列（保留原文/AI 申报保留的行不会被自动重译覆盖）。SKIPPED 不进
        （设计 §14：宁可不翻不可弄坏），TRANSLATED / REVIEWED 不进（断点续传跳过）。
        """
        from pytran.models import SegmentStatus
        return sorted(
            s.uid for s in self.project.segments
            if s.status in (SegmentStatus.PENDING, SegmentStatus.FAILED)
        )

    def _estimate_tokens(self, only_uids: Optional[set] = None) -> int:
        """§15.3 dry-run 估算（复用 cli.estimate_token_budget 的口径）。

        **只对待译行求和**（问题清单 N1）：旧实现传整个 project，
        而已译行的 target 会被全额计入，但引擎根本不会重发它们——
        实测 100 段（50 已译）时估算虚高 2.4 倍（21450 vs 9100），
        导致预算设不准、或被虚假的超限警告骚扰。

        ``only_uids`` 非空时只对这些行求和（试译/失败重试的估算）。
        """
        from pytran.cli import estimate_token_budget
        cfg = self._current_config()
        uids = (set(only_uids) if only_uids is not None
                else set(self._translatable_uids()))
        from pytran.models import Project
        segs = [s for s in self.project.segments if s.uid in uids]
        try:
            return estimate_token_budget(Project(segments=segs), cfg)
        except Exception:  # noqa: BLE001 - 估算失败不应阻断翻译
            return 0

    def _refresh_translate_info(self) -> None:
        """刷新翻译页的「生效参数 + 成本估算」行（§9 L483 / §15.3）。"""
        lbl = self._widgets.get("translate_info")
        warn = self._widgets.get("budget_warn")
        if lbl is None:
            return
        try:
            cfg = self._current_config()
        except Exception:  # noqa: BLE001
            lbl.setText("参数读取失败（请检查 config.ini）")
            return
        estimate = self._estimate_tokens(None)
        # 与 _translatable_uids / _build_queue 同一口径（问题清单 N1）
        pending = len(self._translatable_uids())
        lbl.setText(
            f"模型：{cfg.model or '(未配置)'}"
            f"{' → ' + cfg.fallback_model if cfg.fallback_model else ''}"
            f"  |  并发 {cfg.max_workers}  温度 {cfg.temperature}"
            f"  块大小 {getattr(cfg, 'block_lines', 30)} 行"
            f"  前文 {getattr(cfg, 'context_lines', 3)} 行\n"
            f"待译 {pending} 行，估算约 {estimate:,} token"
            + (f"（预算上限 {self._budget_max:,}）" if self._budget_max else "")
        )
        if warn is not None:
            if self._budget_max is not None and estimate > self._budget_max:
                warn.setText(
                    f"⚠ 估算 {estimate:,} token 超出预算上限 {self._budget_max:,}"
                    "（§15.3）：开始前会再次要求确认。")
                warn.show()
            else:
                warn.hide()

    def _refresh_translate_controls(self) -> None:
        """翻译中/空闲两态的按钮可用性切换。"""
        busy = bool(getattr(self, "_translating", False))
        btn = self._widgets.get("btn_stop")
        if btn is not None:
            btn.setEnabled(busy)
        for key in ("btn_translate", "btn_retry", "btn_budget", "btn_trial"):
            b = self._widgets.get(key)
            if b is not None:
                b.setEnabled(not busy)

    def _trial_translate(self) -> None:
        """R1：试译——只翻少量样本，验证「模型+提示词+术语表+参数」组合。

        设计意图（问题清单 R1）：十万段工程跑了几千行才发现风格不对或
        术语没生效，返工成本极高。先用一个块大小的样本确认组合合适，
        再决定是否全量开跑。

        实现要点：
        * 从未译行（PENDING/FAILED，W5g §2 起不含 KEEP_DECLARED/KEEP_ORIGINAL）
          按 uid 升序取前 N 条——与切块顺序一致，保证带上前文上下文（§4.1）；
        * 走 ``engine.run(only_uids=...)``，与全量**完全同一套校验链**
          （V1-V6 / 检测器），不是简化版；
        * 结果写入工程（project.json），后续全量翻译不会重翻这些行；
        * 完成后**不自动跳页**——试译目的是让人去看结果，不是催他进下一步。
        """
        if not self._ensure_engine_ready():
            return
        if not self.state.has_segments:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self._window, "试译", "工程为空：请先完成「选择目录」和「导入」")
            return

        # 口径与 _build_queue / _estimate_tokens 共用（问题清单 N1）
        translatable = self._translatable_uids()
        if not translatable:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self._window, "试译", "当前没有可试译的行（全部已翻译）。")
            return


        # 试译行数：默认一个块大小（保证是一个完整块、带上下文）
        cfg = self._current_config()
        default_lines = max(1, int(getattr(cfg, "block_lines", 30) or 30))
        default_lines = min(default_lines, len(translatable))
        if self._trial_lines:
            default_lines = min(self._trial_lines, len(translatable))

        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getInt(
            self._window, "试译",
            f"试译行数（共 {len(translatable)} 行待译；\n"
            "建议取一个块大小，以便带上前文上下文）：",
            value=default_lines, min=1, max=len(translatable), step=1)
        if not ok:
            return
        self._trial_lines = int(text)
        picked = set(translatable[:self._trial_lines])

        if not self._confirm_budget(only_uids=picked):
            return
        self._clear_preview()
        self._log(f"试译 {len(picked)} 行（共 {len(translatable)} 行待译）……")
        self._translate_in_background(only_uids=picked, auto_goto=False)

    # ---- R2：最新译文预览 ------------------------------------------------
    def _on_segment_event(self, data: dict) -> None:
        """R2：把一行翻译结果送入「最新译文」预览面板。

        ``segment`` 事件只带 ``uid`` / ``status`` / ``reason``，不带原文译文，
        因此回 ``project.segments`` 取当前值（引擎已把 target 写回 segment，
        且 project.json 是原子写，此处读到的是最新态）。

        只在**翻译运行中**入列：避免用户在审校页手动 accept/flag 时
        也往翻译页预览里堆条目（那会让「试译结果」和「审校操作」混在一起）。
        """
        if not self._translating:
            return
        uid = data.get("uid")
        if uid is None:
            return
        seg = next((s for s in self.project.segments if s.uid == uid), None)
        if seg is None:
            return
        self._append_preview(seg)

    def _on_preview_pause_toggled(self, checked: bool) -> None:
        """暂停/恢复预览面板的自动滚动。"""
        self._preview_paused = bool(checked)
        btn = self._widgets.get("btn_preview_pause")
        if btn is not None:
            btn.setText("恢复滚动" if checked else "暂停滚动")

    def _clear_preview(self) -> None:
        """清空预览面板与计数。"""
        lb = self._widgets.get("preview_list")
        if lb is not None:
            lb.clear()
        self._preview_seen = 0
        self._refresh_preview_count()

    def _refresh_preview_count(self) -> None:
        lbl = self._widgets.get("preview_count")
        if lbl is None:
            return
        lb = self._widgets.get("preview_list")
        shown = lb.count() if lb is not None else 0
        if self._preview_seen == 0:
            lbl.setText("")
        elif shown < self._preview_seen:
            lbl.setText(f"已接收 {self._preview_seen} 条（面板仅保留最近 {shown} 条）")
        else:
            lbl.setText(f"已接收 {self._preview_seen} 条")

    def _append_preview(self, seg) -> None:
        """把一条已译段落进预览面板。

        * 只保留最近 ``_PREVIEW_MAX_ROWS`` 条（十万级长跑时的渲染/内存权衡）；
        * 失败行红色 + 原因；``keep_declared`` 标注「AI 申报保留」；
        * 暂停滚动时仍入列，但不强制滚到底（用户可自己滚动查看）。
        """
        lb = self._widgets.get("preview_list")
        if lb is None:
            return
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QBrush
        from PyQt6.QtWidgets import QListWidgetItem

        status = getattr(seg, "status", None)
        status_name = getattr(status, "name", "") or ""
        target = (seg.target or "").strip()
        source = (seg.source or "").strip()

        if status_name == "FAILED":
            text = f"[失败] {source[:40]}\n    ✗ {seg.error or '未知原因'}"
            color = QColor("#b03030")
        elif status_name == "KEEP_DECLARED":
            text = f"[AI 申报保留] {source[:40]}\n    → {target or '(原文)'}"
            color = QColor("#b8860b")
        elif status_name == "KEEP_ORIGINAL":
            text = f"[保留原文] {source[:40]}\n    → {target or '(原文)'}"
            color = QColor("#b8860b")
        elif status_name in ("TRANSLATED", "REVIEWED"):
            text = f"[{seg.uid}] {source[:40]}\n    → {target}"
            color = QColor("#2e7d32") if status_name == "REVIEWED" else QColor("#1a1a1a")
        else:
            # 其它状态（如 PENDING）不入预览——面板只展示「已译内容」
            return

        item = QListWidgetItem(text)
        item.setForeground(QBrush(color))
        item.setData(Qt.ItemDataRole.UserRole, seg.uid)
        lb.addItem(item)

        # 超过上限时从头部丢弃，避免十万级长跑无限增长
        while lb.count() > _PREVIEW_MAX_ROWS:
            lb.takeItem(0)

        self._preview_seen += 1
        self._refresh_preview_count()
        if not self._preview_paused:
            lb.scrollToBottom()

    def _on_list_select(self, row: int) -> None:
        if row < 0: return
        # 切换条目前，若有未保存修改先暂存，避免误触丢失译文
        self._stash_if_dirty()
        self.state._review_index = row
        self._refresh_editor()

    def _on_editor_changed(self) -> None:
        """译文框内容变化：标记未保存，提示用户。"""
        self._review_dirty = True
        lbl = self._widgets.get("review_dirty")
        if lbl is not None:
            lbl.setText("● 译文已修改，未保存")

    def _stash_if_dirty(self) -> None:
        """若存在未保存的译文修改，先落盘再离开当前条目。"""
        if not getattr(self, "_review_dirty", False):
            return
        ed = self._widgets.get("editor")
        if ed is not None:
            self.state.save_target(ed.toPlainText())
        self._clear_dirty()

    def _clear_dirty(self) -> None:
        self._review_dirty = False
        lbl = self._widgets.get("review_dirty")
        if lbl is not None:
            lbl.setText("")

    def _on_save(self) -> None:
        """独立「保存」：仅写入译文，不改状态、不跳下一条。"""
        ed = self._widgets.get("editor")
        if ed is not None:
            self.state.save_target(ed.toPlainText())
        self._clear_dirty()
        self._log("已保存当前译文。")

    def _on_accept(self) -> None:
        # Save before accept
        ed = self._widgets.get("editor")
        if ed:
            self.state.save_target(ed.toPlainText())
        self._clear_dirty()
        self.state.accept_current()
        self._refresh_review()
        self._navigate_list(1) # Auto move next

    def _on_flag(self) -> None:
        # 失败前也先把未保存的译文修改落盘，避免误触丢失
        self._stash_if_dirty()
        self.state.flag_failed_current("manual")
        self._refresh_review()
        self._navigate_list(1)

    def _on_keep(self) -> None:
        """W5g §2.2「o 保留原文」：标记当前行为保留原文（KEEP_ORIGINAL），
        target 设为原文，且不计入「已译/已审校」完成数。"""
        # 先暂存未保存的译文修改，再标记保留原文（与 _on_flag 一致）
        self._stash_if_dirty()
        if not self.state.keep_original_current():
            self._log("无可保留原文的条目（审校列表为空或非选中项）。", level="warn")
            return
        self._clear_dirty()
        self._refresh_review()
        self._navigate_list(1)

    def _decompile_interpreters(self) -> "tuple[Path | None, Path | None]":
        """反编译用解释器：config.renpy_sdk_dir 的 SDK python 优先（更新版本、
        对新旧 rpyc 兼容性更好），回退游戏自带 python。返回 (python_exe, sdk_root)。"""
        from pytran.cli import build_deps
        cfg = (self.deps.config if self.deps is not None
               else build_deps(self.work_dir, allow_missing_provider=True).config)
        sdk_dir = cfg.sdk_dir()
        if sdk_dir is None:
            return None, None
        for sub in ("lib/py3-windows-x86_64/python.exe",
                    "lib/py2-windows-x86_64/python.exe"):
            exe = sdk_dir / sub
            if exe.exists():
                return exe, sdk_dir
        return None, sdk_dir

    # ---- §8.1 / §8.4 辅助补丁 -------------------------------------------
    def _on_apply_patches(self) -> None:
        """注入勾选的辅助补丁（§8.4：默认不启用、独立勾选、可卸载）。

        * 只写勾中的补丁，未勾选的不碰；
        * 已存在的补丁由 ``UnrenImpl.apply_patch`` 拒绝覆盖（幂等），
          这里把 GameError 转成可读提示；
        * 写入与结果都记日志（§8.4）。
        """
        if not self.project.game_root:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self._window, "辅助补丁",
                "辅助补丁只能用于游戏目录模式。\n请先在「选择目录」中选择游戏根目录。")
            return
        picked = [k for k, cb in self._patch_boxes.items() if cb.isChecked()]
        if not picked:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self._window, "辅助补丁", "请先勾选要注入的补丁。")
            return
        if self.deps is None:
            # 补丁只需 unren，不需要 API Key / provider：
            # 用 allow_missing_provider 避免未配置模式下 OpenAIError。
            self.deps = self._build_default_deps(allow_missing_provider=True)

        written, skipped = [], []
        for key in picked:
            try:
                path = self.deps.unren.apply_patch(
                    Path(self.project.game_root), key)
                written.append(path.name)
                self._log(f"已注入辅助补丁 {path.name}（修改游戏行为，仅调试用）")
            except Exception as exc:  # noqa: BLE001 - GameError 等转可读提示
                skipped.append(f"{key}: {exc}")
                self._log(f"补丁 {key} 注入失败：{exc}", level="warn")

        from PyQt6.QtWidgets import QMessageBox
        msg = []
        if written:
            msg.append("已写入：" + "、".join(written))
        if skipped:
            msg.append("已跳过：\n" + "\n".join(skipped))
        msg.append("这些补丁修改游戏行为，与翻译无关；「移除补丁」可随时卸载。")
        QMessageBox.information(self._window, "辅助补丁", "\n\n".join(msg))
        self._refresh_patch_ui()

    def _on_remove_patches(self) -> None:
        """一键移除全部 unren-*.rpy（§8.4 幂等卸载）。"""
        if not self.project.game_root:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self._window, "辅助补丁",
                "辅助补丁只能用于游戏目录模式。\n请先在「选择目录」中选择游戏根目录。")
            return
        if self.deps is None:
            # 同 _on_apply_patches：移除补丁不需要 provider。
            self.deps = self._build_default_deps(allow_missing_provider=True)
        try:
            removed = self.deps.unren.remove_patches(
                Path(self.project.game_root))
        except Exception as exc:  # noqa: BLE001
            self._log(f"移除补丁失败：{exc}", level="warn")
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self._window, "移除补丁失败", str(exc))
            return

        self._log(f"已移除 {len(removed)} 个辅助补丁"
                  + ("：" + "、".join(p.name for p in removed) if removed else ""))
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(
            self._window, "辅助补丁",
            f"已移除 {len(removed)} 个补丁。" if removed else "没有找到已注入的补丁。")
        self._refresh_patch_ui()

    def _refresh_patch_ui(self) -> None:
        """按当前模式（是否有游戏目录 / 已注入哪些补丁）刷新补丁区。"""
        group = self._widgets.get("patch_group")
        status = self._widgets.get("patch_status")
        if group is None:
            return
        has_game = bool(self.project.game_root)
        group.setEnabled(has_game)

        existing = set()
        if has_game:
            game_dir = Path(self.project.game_root) / "game"
            if game_dir.is_dir():
                existing = {p.stem[len("unren-"):] for p in game_dir.glob("unren-*.rpy")}
        for key, cb in self._patch_boxes.items():
            # 已注入的补丁默认勾上，方便用户看到当前状态并直接再次注入/移除
            if key in existing:
                cb.setChecked(True)
        if status is not None:
            if not has_game:
                status.setText("（仅游戏目录模式可用）")
            elif existing:
                status.setText("已注入：" + "、".join(sorted(existing)))
            else:
                status.setText("尚未注入任何补丁")

    def _on_unpack(self) -> None:
        if self.deps is None:
            self.deps = self._build_default_deps()

        def on_progress(text: str, done: int, total: int) -> None:
            # 后台线程 → 事件队列 → 主线程更新（§9 红线）
            self.on_event("unpack_progress",
                          {"text": f"解包 {text}", "done": done, "total": total})

        def worker() -> None:
            try:
                self.on_event("unpack_progress", {"text": "准备解包……", "done": 0, "total": 0})
                self.on_event("log", {"message": "解包 RPA 中……"})
                extracted = self.deps.unren.extract_rpa(
                    self.project.game_root, progress=on_progress)
                self.on_event("log", {"message": f"解包完成：{len(extracted)} 个文件"})
                py_exe, sdk_root = self._decompile_interpreters()
                if py_exe is not None:
                    self.on_event("log", {"message": f"使用 SDK 解释器：{py_exe}"})
                self.on_event("log", {"message": "反编译 rpyc 中……"})
                # 反编译阶段无逐步进度 → busy 指示器（total=0）
                self.on_event("unpack_progress",
                              {"text": "反编译 rpyc 中……", "done": 0, "total": 0})
                decompiled = self.deps.unren.decompile(
                    self.project.game_root, python_exe=py_exe, sdk_root=sdk_root)
                self.on_event("log",
                              {"message": f"反编译完成：{len(decompiled)} 个 .rpy 生成"})
                self.on_event("unpack_progress",
                              {"text": f"完成：解包 {len(extracted)} 个文件，反编译 {len(decompiled)} 个 .rpy",
                               "done": 100, "total": 100})
                self.on_event("log",
                              {"message": "点「导入」继续（解析刚生成的 rpy）"})
                # N3：与翻译路径一致——后台线程只发事件，切页由主线程执行（§9 红线）
                self.on_event("goto_step", {"step": 1})
                # 解包/反编译在游戏目录里生成了新 .rpy——界面全局刷新一次，
                # 让目录状态标签、置灰规则、审校列表与磁盘实际状态同步。
                self.on_event("refresh_all", {})
            except Exception as exc:  # noqa: BLE001
                self.on_event("unpack_progress",
                              {"text": f"失败：{exc}", "done": 0, "total": 0})
                self.on_event("log", {"message": f"解包/反编译失败：{exc}",
                                      "level": "warn"})
            finally:
                self.on_event("progress", {})

        threading.Thread(target=worker, daemon=True).start()

    def _on_writeback(self) -> None:
        if self.deps is None:
            self.deps = self._build_default_deps()
        journal = getattr(self.deps, "journal", None)
        try:
            self.deps.exporter.write_back_project(self.project, self.work_dir, journal)
            # 写回成功：将导入的文件标记为「已翻译」，并刷新导入页状态列。
            self._written_back_files = {gf.name for gf in self.project.files}
            self._refresh_import_tree()
            self._log("已事务化写回游戏目录")
        except Exception as exc:
            self._log(f"写回失败：{exc}")

    def _refresh_all(self) -> None:
        self._refresh_dir_label()
        self._refresh_scan_tree()
        self._refresh_import_tree()
        self._refresh_steps()
        self._refresh_progress()
        self._refresh_tools()
        self._refresh_patch_ui()
        # 仅当处于审校页（Step 3）时才刷新审校列表，避免工程量大时阻塞启动
        stack = self._widgets.get("stack")
        if stack and stack.currentIndex() == 3:
            self._refresh_review()

    def _refresh_dir_label(self) -> None:
        lbl = self._widgets.get("dir_label")
        if not lbl: return
        text = f"当前工作区: {self.work_dir.absolute()}"
        if self.project.game_root:
            text += f"   |   游戏目录: {self.project.game_root}"
        elif getattr(self, "_pending_rpy", None):
            text += f"   |   选定文件: {self._pending_rpy}"
        lbl.setText(text)

    def _refresh_scan_tree(self) -> None:
        tree = self._widgets.get("scan_tree")
        banner = self._widgets.get("choose_banner")
        if not tree:
            return
        tree.clear()
        
        game_root = self.project.game_root
        from PyQt6.QtWidgets import QTreeWidgetItem
        from PyQt6.QtGui import QColor, QBrush
        
        if not game_root and not getattr(self, "_pending_rpy", None):
            if banner:
                banner.setText("尚未选择游戏目录或脚本文件。请点击上方按钮进行选择。")
                banner.setStyleSheet("padding: 8px; font-weight: bold; background-color: #fff3cd; color: #856404; border-radius: 4px;")
            return
            
        if getattr(self, "_pending_rpy", None) and not game_root:
            p = Path(self._pending_rpy)
            item = QTreeWidgetItem(["RPY 源码", p.name, "就绪：可切换到「导入」页解析"])
            item.setForeground(0, QBrush(QColor("#2e7d32")))
            tree.addTopLevelItem(item)
            if banner:
                banner.setText("✅ 单脚本文件模式已就绪，请点击下一步「导入」开始解析。")
                banner.setStyleSheet("padding: 8px; font-weight: bold; background-color: #d4edda; color: #155724; border-radius: 4px;")
            return

        game = Path(game_root) / "game"
        if not game.is_dir():
            if banner:
                banner.setText("⚠️ 未检测到有效的 game/ 子目录，请检查所选路径是否为 Ren'Py 游戏根目录。")
                banner.setStyleSheet("padding: 8px; font-weight: bold; background-color: #f8d7da; color: #721c24; border-radius: 4px;")
            return

        rpa_files = sorted(game.glob("*.rpa"))
        rpy_files = sorted(game.glob("**/*.rpy"))
        rpyc_files = sorted(game.glob("**/*.rpyc"))
        # .rpyc 只在「无任何 .rpy」时才有提示意义（游戏封包待解包）；
        # 已有 .rpy 时它对翻译流程无意义，不显示。

        for f in rpa_files:
            rel = f.relative_to(game_root).as_posix()
            item = QTreeWidgetItem(["RPA 归档", rel, "需解包（点击下方「解包+反编译」）"])
            item.setForeground(0, QBrush(QColor("#d32f2f")))
            tree.addTopLevelItem(item)

        if not rpy_files:  # 无 .rpy 时才逐条列 rpyc（封包状态的诊断信息）
            for f in rpyc_files:
                rel = f.relative_to(game_root).as_posix()
                item = QTreeWidgetItem(["RPYC 字节码", rel, "需反编译（点击下方「解包+反编译」）"])
                item.setForeground(0, QBrush(QColor("#e65100")))
                tree.addTopLevelItem(item)

        for f in rpy_files:
            rel = f.relative_to(game_root).as_posix()
            item = QTreeWidgetItem(["RPY 源码", rel, "就绪（可直接在「导入」页勾选导入）"])
            item.setForeground(0, QBrush(QColor("#2e7d32")))
            tree.addTopLevelItem(item)

        if banner:
            if rpy_files:
                banner.setText(f"✅ 检测到 {len(rpy_files)} 个 .rpy 源码脚本！已就绪，请点击下一步「导入」进行解析。")
                banner.setStyleSheet("padding: 8px; font-weight: bold; background-color: #d4edda; color: #155724; border-radius: 4px;")
            elif rpa_files or rpyc_files:
                banner.setText(f"⚠️ 发现 {len(rpa_files)} 个 RPA 归档包 / {len(rpyc_files)} 个 rpyc 字节码（游戏处于封包状态），请先点击下方「解包+反编译」！")
                banner.setStyleSheet("padding: 8px; font-weight: bold; background-color: #fff3cd; color: #856404; border-radius: 4px;")
            else:
                banner.setText("未在 game/ 目录下找到任何脚本或归档文件。")
                banner.setStyleSheet("padding: 8px; font-weight: bold; background-color: #f8d7da; color: #721c24; border-radius: 4px;")

    def _refresh_import_tree(self) -> None:
        tree = self._widgets.get("import_tree")
        summary = self._widgets.get("import_summary")
        if not tree:
            return
        from PyQt6.QtWidgets import QTreeWidgetItem
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QBrush

        tree.clear()
        discovered = self._get_discovered_rpy_files()
        imported_files_set = {gf.name for gf in self.project.files}

        for p in discovered:
            game_root = self.project.game_root
            if game_root:
                try:
                    rel = p.relative_to(Path(game_root) / "game").as_posix()
                except ValueError:
                    rel = p.name
            else:
                rel = p.name

            try:
                sz = f"{p.stat().st_size / 1024:.1f} KB"
            except OSError:
                sz = "-"

            is_imported = rel in imported_files_set
            status_text = "已导入" if is_imported else "未导入"

            # 翻译状态（第 5 列）：导入的文件在写回后标记为「已翻译」，
            # 否则「未翻译」；未导入的文件显示「-」占位。
            is_written = rel in self._written_back_files
            if not is_imported:
                trans_text = "-"
            elif is_written:
                trans_text = "已翻译"
            else:
                trans_text = "未翻译"

            item = QTreeWidgetItem(["", rel, sz, status_text, trans_text])
            # 用户需求：导入页勾选默认全不选（用户按需勾选/用「全选」）。
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setData(1, Qt.ItemDataRole.UserRole, str(p))

            if is_imported:
                item.setForeground(3, QBrush(QColor("#2e7d32")))
            else:
                item.setForeground(3, QBrush(QColor("#757575")))
            if trans_text == "已翻译":
                item.setForeground(4, QBrush(QColor("#2e7d32")))
            else:
                item.setForeground(4, QBrush(QColor("#757575")))
            tree.addTopLevelItem(item)

        if summary:
            total_segs = len(self.project.segments)
            summary.setText(f"发现可用脚本：{len(discovered)} 个 | 当前工程已导入段落：{total_segs} 条（未译：{self.project.stats.pending}，跳过：{self.project.stats.skipped}）")

    def _refresh_steps(self) -> None:
        # 所有选项卡均可自由点击切换（选项卡自由导航模式）
        for b in self._widgets.get("steps", []):
            b.setEnabled(True)
        
        stack = self._widgets.get("stack")
        if stack:
            # 自动将视图切换到当前阶段页面，并高亮该选项卡
            target_page = min(self.state.current_step, stack.count() - 1)
            stack.setCurrentIndex(target_page)
            self._highlight_active_step(target_page)

    def _refresh_progress(self) -> None:
        """按 project 状态刷新进度条与文字（§9：done/total/failed）。

        * 空工程显示 0/0，不用 ``or 1`` 掩饰（问题清单 C2）；
        * 恢复确定性模式，避免上一次 round 事件留下的 indeterminate
          状态让 setValue 失效（问题清单 C1）。
        """
        p = self.state.progress()
        total = p["total"]
        pb = self._widgets.get("progress")
        if pb is not None:
            self._set_translate_busy(False)
            if total > 0:
                pb.setValue(int(100.0 * p["done"] / total))
            else:
                pb.setValue(0)
        lbl = self._widgets.get("progress_label")
        if lbl is not None:
            lbl.setText(f"完成 {p['done']}/{p['total']}  失败 {p['failed']}")

    def _refresh_review(self) -> None:
        lb = self._widgets.get("listbox")
        if lb is None:
            return
        
        # Block signals to prevent _on_list_select during population
        lb.blockSignals(True)
        lb.clear()
        
        from PyQt6.QtGui import QColor, QBrush
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QListWidgetItem
        
        items = self.state.review_items()
        total = len(items)
        cap = max(REVIEW_PAGE_SIZE, int(self._review_display_cap))
        shown = items[:cap]
        
        # 游标被筛选/分页裁剪时收回到已渲染范围内，避免选中尾部提示行
        if shown and self.state._review_index >= len(shown):
            self.state._review_index = len(shown) - 1

        for item in shown:
            tag = item.status.name
            text = f"[{tag}] {item.source[:30]}"
            witem = QListWidgetItem(text)
            if tag == "FAILED":
                witem.setForeground(QBrush(QColor("red")))
            elif tag == "TRANSLATED":
                witem.setForeground(QBrush(QColor("blue")))
            elif tag == "REVIEWED":
                witem.setForeground(QBrush(QColor("green")))
            elif tag in ("KEEP_DECLARED", "KEEP_ORIGINAL"):
                # W5g §2：两类保留原文在列表中统一以琥珀色标记（按"不区分"）
                witem.setForeground(QBrush(QColor("#b8860b")))
                
            lb.addItem(witem)

        if total > cap:
            hint_item = QListWidgetItem(
                f"…… 仅渲染前 {cap} 条（当前筛选共 {total} 条）；"
                f"可换筛选条件，或点「显示更多」继续加载")
            hint_item.setForeground(QBrush(QColor("#888888")))
            # 提示行不可选中/不可导航，避免污染审校游标
            hint_item.setFlags(Qt.ItemFlag.NoItemFlags)
            lb.addItem(hint_item)
            
        if lb.count() > 0:
            idx = min(self.state._review_index, max(len(shown) - 1, 0))
            lb.setCurrentRow(idx)
            
        lb.blockSignals(False)
        
        pos = self._widgets.get("review_position")
        if pos is not None:
            if total == 0:
                pos.setText("当前筛选下没有条目")
            else:
                pos.setText(
                    f"当前筛选 {total} 条，已渲染 {len(shown)} 条，"
                    f"正在审校第 {self.state._review_index + 1} 条")
        btn_more = self._widgets.get("btn_load_more")
        if btn_more is not None:
            btn_more.setEnabled(total > cap)
        
        self._refresh_editor()

    def _refresh_editor(self) -> None:
        ed = self._widgets.get("editor")
        if ed is None:
            return
        cur = self.state.current_review()
        # 右侧上方原文全文：跟随选中条目刷新
        sv = self._widgets.get("source_view")
        if sv is not None:
            sv.clear()
            if cur is not None:
                sv.setPlainText(cur.source or "")
        # 载入条目时屏蔽 textChanged，避免把"加载内容"误判为"用户修改"
        ed.blockSignals(True)
        ed.clear()
        if cur is not None:
            ed.setPlainText(cur.target or "")
        ed.blockSignals(False)
        self._clear_dirty()

    def _refresh_tools(self) -> None:
        ctrl = self.state.all_controls()
        if "btn_unpack" in self._widgets:
            self._widgets["btn_unpack"].setEnabled(ctrl["tools_unpack"])
        if "btn_writeback" in self._widgets:
            self._widgets["btn_writeback"].setEnabled(ctrl["tools_writeback"])
        if "btn_translate" in self._widgets:
            self._widgets["btn_translate"].setEnabled(ctrl["translate"])
        if "btn_retry" in self._widgets:
            self._widgets["btn_retry"].setEnabled(ctrl["retry_failed"])
        if "btn_export_csv" in self._widgets:
            self._widgets["btn_export_csv"].setEnabled(ctrl["export_writeback"])
        # 翻译页的参数/成本行与按钮 busy 态
        self._refresh_translate_info()
        self._refresh_translate_controls()

    def _log(self, message: str, level: str = "info") -> None:
        box = self._widgets.get("log")
        if box is None:
            return
            
        from PyQt6.QtGui import QColor
        
        if level == "warn":
            box.setTextColor(QColor("#b03030"))
        else:
            box.setTextColor(QColor("black"))
            
        box.append(message)


def launch(project_path: Optional[Path] = None, work_dir: Optional[Path] = None) -> None:
    wd = Path(work_dir or ".")
    store = JsonProjectStore()
    project = store.load(wd)
    app = PytranApp(project, work_dir=wd)
    app.run()
