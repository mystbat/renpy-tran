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
from pytran.ui_state import UiState

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict], None]

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
        from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QProgressBar, QLabel, QListWidget, QTextEdit, QSplitter, QStackedWidget
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
        
        self._widgets["btn_unpack"] = QPushButton("解包+反编译 (如果是未解包的 rpa/rpyc 游戏，点击此项)")
        self._widgets["btn_unpack"].clicked.connect(self._on_unpack)
        choose_layout.addWidget(self._widgets["btn_unpack"])
        choose_layout.addStretch()
        self._widgets["stack"].addWidget(page_choose)
        
        # --- Page 1: Import ---
        page_import = QWidget()
        import_layout = QVBoxLayout(page_import)
        import_layout.addWidget(QLabel("第二步：扫描并解析目录下的脚本文件。"))
        self._widgets["btn_import"] = QPushButton("开始解析/导入")
        self._widgets["btn_import"].clicked.connect(self._import_rpy)
        import_layout.addWidget(self._widgets["btn_import"])
        import_layout.addStretch()
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

    def _navigate_list(self, delta: int) -> None:
        lb = self._widgets.get("listbox")
        if not lb: return
        count = lb.count()
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
            self._pending_rpy = rpy
            self.state.goto(1)
            self._log(f"已选择 rpy 文件：{rpy}")
            self._log("点「导入」开始解析")

    def _import_rpy(self) -> None:
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
        if game_root:
            tl = Path(game_root) / "game" / "tl"
            pattern = tl if tl.is_dir() else Path(game_root) / "game"
            for rpy in sorted(pattern.glob("**/*.rpy")):
                rel = rpy.relative_to(Path(game_root) / "game").as_posix()
                segs.extend(parser.parse_file(rpy, game_relpath=rel))
                files.append((rel, rpy))
            if not segs:
                game = Path(game_root) / "game"
                n_rpa = len(list(game.glob("*.rpa"))) if game.is_dir() else 0
                n_rpyc = len(list(game.glob("**/*.rpyc"))) if game.is_dir() else 0
                if n_rpa or n_rpyc:
                    self._log("未找到 .rpy，但发现未解包的 RPA/rpyc "
                              f"（{n_rpa} RPA / {n_rpyc} rpyc）"
                              "→ 请先点「解包+反编译」，完成后再回来「导入」")
                else:
                    self._log(f"在 {pattern} 未找到任何 .rpy / .rpyc / .rpa；"
                              "请确认这是游戏根目录，或用 Ren'Py SDK 生成翻译骨架")
                return
        elif getattr(self, "_pending_rpy", None):
            rpy = Path(self._pending_rpy)
            segs.extend(parser.parse_file(rpy, game_relpath=rpy.name))
            files.append((rpy.name, rpy))
        else:
            QMessageBox.information(self._window, "导入", "请先在「选择目录」中指定游戏目录或 rpy 文件")
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
        self._log(f"导入完成：{len(segs)} 段（skipped {self.project.stats.skipped}）→ project.json 已保存")
        self._log("点「翻译」开始（请确认已在 ⚙ 设置中填好 API Key）")

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
        self._refresh_steps()
        self._refresh_progress()
        self._refresh_review()
        self._refresh_tools()

    def _refresh_dir_label(self) -> None:
        lbl = self._widgets.get("dir_label")
        if not lbl: return
        text = f"当前工作区: {self.work_dir.absolute()}"
        if self.project.game_root:
            text += f"   |   游戏目录: {self.project.game_root}"
        elif getattr(self, "_pending_rpy", None):
            text += f"   |   选定文件: {self._pending_rpy}"
        lbl.setText(text)

    def _refresh_steps(self) -> None:
        for i, b in enumerate(self._widgets.get("steps", [])):
            b.setEnabled(i <= self.state.current_step)
        
        stack = self._widgets.get("stack")
        if stack:
            if self.state.current_step == 3:
                stack.setCurrentIndex(1)
            else:
                stack.setCurrentIndex(0)

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
        from PyQt6.QtWidgets import QListWidgetItem
        
        for item in self.state.review_items():
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
            
        if lb.count() > 0:
            idx = min(self.state._review_index, lb.count() - 1)
            lb.setCurrentRow(idx)
            
        lb.blockSignals(False)
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
        if "unpack" in self._widgets:
            self._widgets["unpack"].setEnabled(ctrl["tools_unpack"])
        if "writeback" in self._widgets:
            self._widgets["writeback"].setEnabled(ctrl["tools_writeback"])

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
