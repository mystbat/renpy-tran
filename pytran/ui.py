"""GUI 视图层（基于 PyQt6，重构版本）。

* 延迟 import PyQt6，便于无头环境 import 与测试。
* 视图是 UiState 的薄壳。
* 后台线程发事件到 queue，主线程定时消费。
"""

from __future__ import annotations

import logging
import queue
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
    "reviewed": "已审校",
}

# 审校列表单次渲染的上限：`显示更多` 每次追加这么多条。
# 海量大工程（十万级）一次创建全部 Qt 条目会让窗口假死（实测 13.5 万条约 11 秒）。
REVIEW_PAGE_SIZE = 1000

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
        # 审校列表当前已渲染的条目上限（随「显示更多」递增）
        self._review_display_cap = REVIEW_PAGE_SIZE

    def on_event(self, name: str, data: dict) -> None:
        self._events.put((name, data or {}))

    def _drain_events(self) -> None:
        try:
            while True:
                name, data = self._events.get_nowait()
                self._apply_event(name, data)
        except queue.Empty:
            pass

    def _apply_event(self, name: str, data: dict) -> None:
        if name == "progress":
            self._refresh_progress()
        elif name == "segment":
            self._refresh_review()
        elif name == "log":
            self._log(data.get("message", ""), data.get("level", "info"))
        elif name == "unpack_progress":
            pb = self._widgets.get("progress")
            lbl = self._widgets.get("progress_label")
            total = data.get("total") or 0
            if pb is not None:
                if total <= 0:
                    pb.setRange(0, 0)  # busy 指示器（反编译阶段无逐步进度）
                else:
                    pb.setRange(0, 100)
                    pb.setValue(int(100.0 * data.get("done", 0) / total))
            if lbl is not None:
                lbl.setText(data.get("text", ""))

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

    def _translate_in_background(self) -> None:
        if not self._ensure_engine_ready():
            self.on_event("progress", {})
            return

        def worker() -> None:
            try:
                deps = self.deps
                deps.engine.run(self.project, deps.config, self._load_profile(),
                                on_event=self.on_event)
                self.state.goto(3)
                # Ensure UI refreshes
                self.on_event("progress", {})
                self.on_event("segment", {}) # Trigger review refresh
            except Exception as exc:
                self.on_event("log", {"message": f"翻译失败：{exc}"})
            finally:
                self.on_event("progress", {})

        threading.Thread(target=worker, daemon=True).start()

    def _build_default_deps(self) -> object:
        from pytran.cli import build_deps
        return build_deps(self.work_dir)

    def _load_profile(self) -> TermProfile:
        return TermProfile.load(self.work_dir / self.state.project.term_profile)

    def run(self) -> None:  # pragma: no cover
        import sys
        from PyQt6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QPushButton, QProgressBar, QLabel, QListWidget, QTextEdit,
            QSplitter, QStackedWidget, QTreeWidget, QTreeWidgetItem,
            QHeaderView, QComboBox
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
        import_layout.addLayout(import_bar)
        
        # 待导入脚本树状/勾选表格
        self._widgets["import_tree"] = QTreeWidget()
        self._widgets["import_tree"].setHeaderLabels(["选择", "相对路径", "文件大小", "导入状态"])
        self._widgets["import_tree"].header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        import_layout.addWidget(self._widgets["import_tree"], stretch=1)
        
        self._widgets["import_summary"] = QLabel("就绪")
        import_layout.addWidget(self._widgets["import_summary"])
        self._widgets["stack"].addWidget(page_import)
        
        # --- Page 2: Translate ---
        page_translate = QWidget()
        translate_layout = QVBoxLayout(page_translate)
        translate_layout.addWidget(QLabel("第三步：自动汉化进程。"))
        
        progress_layout = QVBoxLayout()
        self._widgets["progress"] = QProgressBar()
        self._widgets["progress"].setRange(0, 100)
        progress_layout.addWidget(self._widgets["progress"])
        self._widgets["progress_label"] = QLabel("准备就绪")
        progress_layout.addWidget(self._widgets["progress_label"])
        translate_layout.addLayout(progress_layout)
        
        translate_btns = QHBoxLayout()
        self._widgets["btn_translate"] = QPushButton("开始自动翻译")
        self._widgets["btn_translate"].clicked.connect(self._start_translate)
        translate_btns.addWidget(self._widgets["btn_translate"])
        
        self._widgets["btn_retry"] = QPushButton("重试失败项")
        self._widgets["btn_retry"].clicked.connect(self._start_translate)
        translate_btns.addWidget(self._widgets["btn_retry"])
        translate_layout.addLayout(translate_btns)
        translate_layout.addStretch()
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
        
        self._widgets["editor"] = QTextEdit()
        splitter.addWidget(self._widgets["editor"])
        splitter.setSizes([300, 800])
        review_layout.addWidget(splitter, stretch=1)
        
        review_btns = QHBoxLayout()
        btn_accept = QPushButton("a 通过(Ctrl+S)")
        btn_accept.clicked.connect(self._on_accept)
        btn_flag = QPushButton("f 失败")
        btn_flag.clicked.connect(self._on_flag)
        review_btns.addWidget(btn_accept)
        review_btns.addWidget(btn_flag)
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
            _scoped("J", lambda: self._navigate_list(1))
            _scoped("K", lambda: self._navigate_list(-1))
        sc_save = QShortcut(QKeySequence("Ctrl+S"), window)
        sc_save.activated.connect(self._on_accept)

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
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QPushButton, QVBoxLayout, QMessageBox
        
        dlg = QDialog(self._window)
        dlg.setWindowTitle("设置")
        dlg.setModal(True)
        dlg.resize(500, 300)
        
        cfg = self._build_default_deps().config if self.deps is None else self.deps.config
        
        layout = QVBoxLayout(dlg)
        form = QFormLayout()
        
        fields = [
            ("API Key（存环境变量持久化请用系统设置）", "api_key", True),
            ("模型地址 (URL)", "api_url", False),
            ("模型名称", "model", False),
            ("Ren'Py SDK 目录（可选，生成 tl 骨架）", "renpy_sdk_dir", False),
            ("目标语言", "language", False),
        ]
        entries = {}
        for label, attr, secret in fields:
            e = QLineEdit(str(getattr(cfg, attr, "") or ""))
            if secret:
                e.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(label, e)
            entries[attr] = e
            
        layout.addLayout(form)
        
        def save() -> None:
            for attr, e in entries.items():
                setattr(cfg, attr, e.text().strip())
            cfg.save(self.work_dir)
            if cfg.api_key and (self.deps is None or self.deps.engine is None):
                try:
                    self.deps = self._build_default_deps()
                    self._log("依赖已就绪：可以开始翻译")
                except Exception as exc:
                    self._log(f"依赖重建失败：{exc}")
            QMessageBox.information(dlg, "设置", "已保存到 config.ini（API Key 不会写入文件，\n持久化请将其设为系统环境变量 TL_API_KEY）")
            dlg.accept()
            
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(save)
        layout.addWidget(btn_save)
        
        dlg.exec()

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
        """选择单个 .rpy 文件（纯文件模式），委托给目录选择的文件分支。"""
        from PyQt6.QtWidgets import QFileDialog
        rpy, _ = QFileDialog.getOpenFileName(
            self._window, "选择单个 .rpy 文件（纯文件模式）",
            str(self.work_dir), "Ren'Py 脚本 (*.rpy);;所有文件 (*.*)")
        if rpy:
            self._pending_rpy = rpy
            self.state.goto(1)
            self._log(f"已选择 rpy 文件：{rpy}")
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
                self._log(f"已切换游戏目录（{old_root or '无'} → {game_dir}），"
                          "旧导入数据已清空")
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

    def _import_rpy(self) -> None:
        # 全部导入：扫描候选清单并全部导入
        all_files = self._get_discovered_rpy_files()
        if not all_files:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self._window, "导入", "未发现可供导入的 .rpy 文件，请先在「选择目录」中选择游戏或解包。")
            return
        self._do_import_files(all_files)

    def _get_discovered_rpy_files(self) -> list[Path]:
        files: list[Path] = []
        game_root = self.project.game_root
        if game_root:
            tl = Path(game_root) / "game" / "tl"
            pattern = tl if tl.is_dir() else Path(game_root) / "game"
            files = sorted(pattern.glob("**/*.rpy"))
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
        self.state.goto(2)
        self._translate_in_background()

    def _on_list_select(self, row: int) -> None:
        if row < 0: return
        self.state._review_index = row
        self._refresh_editor()

    def _on_accept(self) -> None:
        # Save before accept
        ed = self._widgets.get("editor")
        if ed:
            self.state.save_target(ed.toPlainText())
        self.state.accept_current()
        self._refresh_review()
        self._navigate_list(1) # Auto move next

    def _on_flag(self) -> None:
        self.state.flag_failed_current("manual")
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

    def _on_unpack(self) -> None:
        if self.deps is None:
            self.deps = self._build_default_deps()

        def on_progress(text: str, done: int, total: int) -> None:
            # 后台线程 → 事件队列 → 主线程更新（§9 红线）
            self.on_event("unpack_progress",
                          {"text": f"解包 {text}", "done": done, "total": total})

        def worker() -> None:
            try:
                self.on_event("log", {"message": "解包 RPA 中……"})
                extracted = self.deps.unren.extract_rpa(
                    self.project.game_root, progress=on_progress)
                self.on_event("log", {"message": f"解包完成：{len(extracted)} 个文件"})
                py_exe, sdk_root = self._decompile_interpreters()
                if py_exe is not None:
                    self.on_event("log", {"message": f"使用 SDK 解释器：{py_exe}"})
                self.on_event("log", {"message": "反编译 rpyc 中……"})
                decompiled = self.deps.unren.decompile(
                    self.project.game_root, python_exe=py_exe, sdk_root=sdk_root)
                self.on_event("log",
                              {"message": f"反编译完成：{len(decompiled)} 个 .rpy 生成"})
                self.on_event("log",
                              {"message": "点「导入」继续（解析刚生成的 rpy）"})
                self.state.goto(1)
            except Exception as exc:  # noqa: BLE001
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
        rpyc_files = sorted(game.glob("**/*.rpyc"))
        rpy_files = sorted(game.glob("**/*.rpy"))

        for f in rpa_files:
            rel = f.relative_to(game_root).as_posix()
            item = QTreeWidgetItem(["RPA 归档", rel, "需解包（点击下方「解包+反编译」）"])
            item.setForeground(0, QBrush(QColor("#d32f2f")))
            tree.addTopLevelItem(item)

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

            item = QTreeWidgetItem(["", rel, sz, status_text])
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(1, Qt.ItemDataRole.UserRole, str(p))

            if is_imported:
                item.setForeground(3, QBrush(QColor("#2e7d32")))
            else:
                item.setForeground(3, QBrush(QColor("#757575")))
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
        p = self.state.progress()
        pb = self._widgets.get("progress")
        if pb is not None:
            total = p["total"] or 1
            pb.setValue(int(100.0 * p["done"] / total))
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
        ed.clear()
        if cur is not None:
            ed.setPlainText(cur.target or "")

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
