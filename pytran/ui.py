"""GUI 视图层（W5 交付物，设计 §9）。

* 本文件**延迟** import tkinter，导入 pytran.ui 不会触碰 GUI 栈，便于无头环境
  import 与测试；真正建窗在 ``PytranApp.run()`` 内。
* 视图只是 ``UiState``（§9 状态机）的薄壳：控件置灰、进度条、审校页导航全部
  委托给 ``ui_state``，本层只做「状态 → 控件」的渲染与「控件 → 状态」的事件。
* 硬规则（§9）：后台线程只发事件（经 ``root.after`` 回主线程），所有 Tk 调用
  只在主线程；``on_event`` 默认实现即把事件投递到主线程回调，不在子线程改控件。
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

# 事件队列：后台线程只 put，主线程消费（§9「后台线程只发事件」）。
EventCallback = Callable[[str, dict], None]


class PytranApp:
    """单窗口 Tk 应用（§9 布局）。

    构造参数：``project`` 已加载的 Project；``state`` 可选注入（测试/复用状态机）；
    ``deps`` 提供 translate/write_back 等副作用（默认用 cli.build_deps 组装的生产依赖）。
    """

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
        # 主线程消费：后台线程 put，run() 启动时开定时器 drain
        self._events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._root = None
        self._widgets: dict = {}

    # -- 事件桥（§9：后台只发事件） -----------------------------------------
    def on_event(self, name: str, data: dict) -> None:
        """供引擎/后台任务回调：仅入队，不碰任何 Tk 对象。"""
        self._events.put((name, data or {}))

    def _drain_events(self) -> None:
        """主线程定时器：消费事件队列并更新 UI。"""
        try:
            while True:
                name, data = self._events.get_nowait()
                self._apply_event(name, data)
        except queue.Empty:
            pass
        if self._root is not None:
            self._root.after(100, self._drain_events)

    def _apply_event(self, name: str, data: dict) -> None:
        if name == "progress":
            self._refresh_progress()
        elif name == "segment":
            self._refresh_review()
        elif name == "log":
            self._log(data.get("message", ""))
        # 其它事件（round/started/...）忽略或扩展

    # -- 后台翻译（子线程只发事件，不碰控件） -------------------------------
    def _ensure_engine_ready(self) -> bool:
        """翻译前检查引擎依赖；未配置（无 Key）时引导设置并返回 False。"""
        if self.deps is not None and self.deps.engine is not None:
            return True
        try:
            self.deps = self._build_default_deps()
        except Exception as exc:  # noqa: BLE001
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
                self.state.goto(3)  # 翻译完进审校
            except Exception as exc:  # noqa: BLE001 - 经事件回主线程
                self.on_event("log", {"message": f"翻译失败：{exc}"})
            finally:
                self.on_event("progress", {})

        threading.Thread(target=worker, daemon=True).start()

    def _build_default_deps(self) -> object:
        from pytran.cli import build_deps
        return build_deps(self.work_dir)

    def _load_profile(self) -> TermProfile:
        return TermProfile.load(self.work_dir / self.state.project.term_profile)

    # -- 视图构建（主线程唯一入口） -----------------------------------------
    def run(self) -> None:  # pragma: no cover - 需要真实显示
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title("pytran 3.0")
        self._root = root
        self._build_widgets(root, ttk)
        self._refresh_all()
        root.after(100, self._drain_events)
        root.mainloop()

    def _build_widgets(self, root, ttk) -> None:
        # §9 顶部五步条 + 设置入口
        steps = ["选择目录", "导入", "翻译", "审校", "导出写回"]
        step_frame = ttk.Frame(root)
        step_frame.grid(row=0, column=0, sticky="ew")
        self._widgets["steps"] = []
        for i, label in enumerate(steps):
            b = ttk.Button(step_frame, text=label,
                           command=lambda idx=i: self._on_step(idx))
            b.grid(row=0, column=i, padx=2)
            self._widgets["steps"].append(b)
        ttk.Button(step_frame, text="⚙ 设置",
                   command=self._open_settings).grid(row=0, column=len(steps), padx=(8, 2))

        # §9 进度条
        self._widgets["progress"] = ttk.Progressbar(root, length=300, mode="determinate")
        self._widgets["progress"].grid(row=1, column=0, sticky="ew", pady=2)
        self._widgets["progress_label"] = ttk.Label(root, text="")
        self._widgets["progress_label"].grid(row=2, column=0, sticky="w")

        # §9 审校页：左列表 + 右编辑框
        review = ttk.Frame(root)
        review.grid(row=3, column=0, sticky="nsew")
        self._widgets["listbox"] = tk.Listbox(review, height=10)
        self._widgets["listbox"].grid(row=0, column=0, sticky="ns")
        self._widgets["listbox"].bind("<<ListboxSelect>>", self._on_list_select)
        self._widgets["editor"] = tk.Text(review, height=6, width=40)
        self._widgets["editor"].grid(row=0, column=1, sticky="nsew")
        ttk.Button(review, text="a 通过(Ctrl+S)", command=self._on_accept).grid(row=1, column=0)
        ttk.Button(review, text="f 失败", command=self._on_flag).grid(row=1, column=1)

        # §9 工具区
        tools = ttk.Frame(root)
        tools.grid(row=4, column=0, sticky="ew")
        self._widgets["unpack"] = ttk.Button(tools, text="解包+反编译",
                                              command=self._on_unpack)
        self._widgets["unpack"].grid(row=0, column=0)
        self._widgets["writeback"] = ttk.Button(tools, text="写回游戏",
                                                command=self._on_writeback)
        self._widgets["writeback"].grid(row=0, column=1)

        # §9 日志面板
        self._widgets["log"] = tk.Text(root, height=6, state="disabled")
        self._widgets["log"].grid(row=5, column=0, sticky="ew")

    # -- 设置对话框（§9：API Key/模型/SDK/语言） -----------------------------
    def _open_settings(self) -> None:
        import tkinter as tk
        from tkinter import ttk, messagebox

        dlg = tk.Toplevel(self._root)
        dlg.title("设置")
        dlg.grab_set()
        cfg = self._build_default_deps().config if self.deps is None else self.deps.config

        fields = [
            ("API Key（存环境变量持久化请用系统设置）", "api_key", True),
            ("模型地址 (URL)", "api_url", False),
            ("模型名称", "model", False),
            ("Ren'Py SDK 目录（可选，生成 tl 骨架）", "renpy_sdk_dir", False),
            ("目标语言", "language", False),
        ]
        entries: dict[str, ttk.Entry] = {}
        for i, (label, attr, secret) in enumerate(fields):
            ttk.Label(dlg, text=label).grid(row=i, column=0, sticky="w", padx=6, pady=3)
            e = ttk.Entry(dlg, width=52, show="*" if secret else "")
            e.insert(0, str(getattr(cfg, attr, "") or ""))
            e.grid(row=i, column=1, padx=6, pady=3)
            entries[attr] = e

        def save() -> None:
            for attr, e in entries.items():
                setattr(cfg, attr, e.get().strip())
            cfg.save(self.work_dir)
            # Key 已填 → 立即重建依赖（provider/engine），无需重启 GUI
            if cfg.api_key and (self.deps is None or self.deps.engine is None):
                try:
                    self.deps = self._build_default_deps()
                    self._log("依赖已就绪：可以开始翻译")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"依赖重建失败：{exc}")
            messagebox.showinfo("设置", "已保存到 config.ini（API Key 不会写入文件，\n"
                                        "持久化请将其设为系统环境变量 TL_API_KEY）")
            dlg.destroy()

        ttk.Button(dlg, text="保存", command=save).grid(
            row=len(fields), column=0, columnspan=2, pady=8)

    # -- 控件 → 状态 ---------------------------------------------------------
    def _on_step(self, idx: int) -> None:
        self.state.goto(idx)
        self._refresh_steps()

    def _on_list_select(self, _evt) -> None:
        self.state._review_index = self._widgets["listbox"].curselection()[0] \
            if self._widgets["listbox"].curselection() else 0
        self._refresh_editor()

    def _on_accept(self) -> None:
        self.state.accept_current()
        self._refresh_review()

    def _on_flag(self) -> None:
        self.state.flag_failed_current("manual")
        self._refresh_review()

    def _on_unpack(self) -> None:
        # 游戏目录下的解包/反编译；无 game_root 时按钮本就置灰（§9）
        if self.deps is None:
            self.deps = self._build_default_deps()
        try:
            self.deps.unren.extract_rpa(self.project.game_root)
            self.deps.unren.decompile(self.project.game_root)
        except Exception as exc:  # noqa: BLE001
            self._log(f"解包失败：{exc}")

    def _on_writeback(self) -> None:
        # L0 写回需人工确认；此处直接走 exporter 事务化写回（§15.2）
        if self.deps is None:
            self.deps = self._build_default_deps()
        journal = getattr(self.deps, "journal", None)
        try:
            self.deps.exporter.write_back_project(self.project, self.work_dir, journal)
            self._log("已事务化写回游戏目录")
        except Exception as exc:  # noqa: BLE001
            self._log(f"写回失败：{exc}")

    # -- 状态 → 控件（主线程渲染） ------------------------------------------
    def _refresh_all(self) -> None:
        self._refresh_steps()
        self._refresh_progress()
        self._refresh_review()
        self._refresh_tools()

    def _refresh_steps(self) -> None:
        for i, b in enumerate(self._widgets.get("steps", [])):
            b.config(state="normal" if i <= self.state.current_step else "disabled")

    def _refresh_progress(self) -> None:
        p = self.state.progress()
        pb = self._widgets.get("progress")
        if pb is not None:
            total = p["total"] or 1
            pb["value"] = 100.0 * p["done"] / total
        lbl = self._widgets.get("progress_label")
        if lbl is not None:
            lbl.config(text=f"完成 {p['done']}/{p['total']}  失败 {p['failed']}")

    def _refresh_review(self) -> None:
        lb = self._widgets.get("listbox")
        if lb is None:
            return
        lb.delete(0, "end")
        for item in self.state.review_items():
            tag = item.status.name
            lb.insert("end", f"[{tag}] {item.source[:30]}")
        self._refresh_editor()

    def _refresh_editor(self) -> None:
        ed = self._widgets.get("editor")
        if ed is None:
            return
        cur = self.state.current_review()
        ed.delete("1.0", "end")
        if cur is not None:
            ed.insert("1.0", cur.target or "")

    def _refresh_tools(self) -> None:
        ctrl = self.state.all_controls()
        if "unpack" in self._widgets:
            self._widgets["unpack"].config(
                state="normal" if ctrl["tools_unpack"] else "disabled")
        if "writeback" in self._widgets:
            self._widgets["writeback"].config(
                state="normal" if ctrl["tools_writeback"] else "disabled")
        if "translate" in self._widgets:
            pass  # 翻译按钮由 run 内事件驱动

    def _log(self, message: str) -> None:
        box = self._widgets.get("log")
        if box is None:
            return
        box.config(state="normal")
        box.insert("end", message + "\n")
        box.config(state="disabled")


def launch(project_path: Optional[Path] = None, work_dir: Optional[Path] = None) -> None:
    """GUI 入口：加载 project.json 并启动主窗（§9 默认 L0 交互）。"""
    wd = Path(work_dir or ".")
    store = JsonProjectStore()
    project = store.load(wd)
    app = PytranApp(project, work_dir=wd)
    app.run()
