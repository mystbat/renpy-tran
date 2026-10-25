"""pytest 共享夹具（T0 骨架；各工作包可在此追加自己的 fixture）。

注意：夹具只放测试基础设施（临时目录、mock provider 等），
不得引入被测模块的业务逻辑。
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

import pytest
from _pytest import pathlib as _pytest_pathlib
from _pytest.tmpdir import (
    LOCK_TIMEOUT,
    TempPathFactory,
    get_user,
    get_user_id,
    make_numbered_dir_with_cleanup,
    rm_rf,
)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """标准工作目录布局（设计文档 §2.2）。"""
    for sub in ("bak", "output", "logs"):
        (tmp_path / sub).mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# 防误清空守卫（2026-10-25 事故回归）
#
# 背景：真实应用的 work_dir 就是仓库根（``运行.bat`` 传 ``--work-dir "%~dp0."``），
# 而仓库根正是用户自己的 project.json / profile.json / glossary.csv 所在处。
# 历史上测试里写过 ``PytranApp(proj, work_dir=".")``，一旦被测代码调用
# ``JsonProjectStore().save(...)`` 或 ``TermProfile.save(...)``，就会用测试用的
# 空工程覆盖用户的真实数据（2026-10-25 已发生一次：12099 段 + 118 条术语被清空）。
#
# 守卫策略：任何测试只要试图把 project.json / profile.json 写进仓库根，
# 立即 fail 并给出明确提示，改用 ``tmp_path``。
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_repo_root(path) -> bool:
    try:
        return Path(path).resolve() == _REPO_ROOT
    except OSError:  # pragma: no cover - 路径不可解析时不拦
        return False


@pytest.fixture(autouse=True)
def _forbid_writes_into_repo_root(monkeypatch):
    """自动生效：禁止测试把工程数据写进仓库根。"""
    from pytran.project import JsonProjectStore

    real_save = JsonProjectStore.save

    def guarded_save(self, project, work_dir):  # noqa: ANN001
        if _is_repo_root(work_dir):
            raise AssertionError(
                "测试试图把 project.json 写进仓库根 "
                f"({_REPO_ROOT})——那里是用户的真实工程数据。"
                "请给 PytranApp / JsonProjectStore.save 传 tmp_path。"
            )
        return real_save(self, project, work_dir)

    monkeypatch.setattr(JsonProjectStore, "save", guarded_save, raising=False)

    try:
        from pytran.config import TermProfile
    except Exception:  # pragma: no cover - 模块不可用时跳过该分支
        TermProfile = None

    if TermProfile is not None and hasattr(TermProfile, "save"):
        real_profile_save = TermProfile.save

        def guarded_profile_save(self, path):  # noqa: ANN001
            parent = Path(path).resolve().parent
            if parent == _REPO_ROOT:
                raise AssertionError(
                    f"测试试图把 profile.json 写进仓库根 ({_REPO_ROOT})——"
                    "那里是用户的真实术语表/翻译规则。请传 tmp_path。"
                )
            return real_profile_save(self, path)

        monkeypatch.setattr(TermProfile, "save", guarded_profile_save, raising=False)

    # BUGFIX(profile-persist, 2026-10-25)：术语/规则现在还会以镜像形式写到
    # glossary.csv / rules.txt / exempt.txt，而 write_mirrors / save_profile
    # 是直接 path.write_text，绕过了上面 TermProfile.save 的守卫。
    # 这里把 profile_store 的写入口也一并拦下，否则一个 work_dir="." 的测试
    # 就能把用户的真实术语表和翻译规则镜像覆盖掉。
    try:
        from pytran import profile_store as _profile_store
    except Exception:  # pragma: no cover - 模块不可用时跳过
        _profile_store = None

    if _profile_store is not None:
        _real_save_profile = _profile_store.save_profile
        _real_write_mirrors = _profile_store.write_mirrors

        def _reject_repo_root(work_dir, what: str) -> None:
            if _is_repo_root(work_dir):
                raise AssertionError(
                    f"测试试图把{what}写进仓库根 ({_REPO_ROOT})——"
                    "那里是用户的真实术语表/翻译规则。请传 tmp_path。"
                )

        def guarded_save_profile(profile, work_dir, filename="profile.json"):  # noqa: ANN001
            _reject_repo_root(work_dir, " profile.json")
            return _real_save_profile(profile, work_dir, filename)

        def guarded_write_mirrors(profile, work_dir):  # noqa: ANN001
            _reject_repo_root(work_dir, "术语/规则镜像")
            return _real_write_mirrors(profile, work_dir)

        monkeypatch.setattr(
            _profile_store, "save_profile", guarded_save_profile, raising=False)
        monkeypatch.setattr(
            _profile_store, "write_mirrors", guarded_write_mirrors, raising=False)

    try:
        from pytran.ui import PytranApp
    except Exception:  # pragma: no cover - 无 PyQt6 环境
        yield
        return

    real_init = PytranApp.__init__

    def guarded_init(self, project, **kwargs):  # noqa: ANN001
        wd = kwargs.get("work_dir")
        if wd is not None and _is_repo_root(wd):
            raise AssertionError(
                'PytranApp(work_dir=".") 会指向仓库根，从而污染用户真实工程数据。'
                "请改用 work_dir=str(tmp_path)。"
            )
        return real_init(self, project, **kwargs)

    monkeypatch.setattr(PytranApp, "__init__", guarded_init, raising=False)
    yield


@pytest.fixture
def profile() -> "TermProfile":
    """空术语表（无豁免）。"""
    from pytran.config import TermProfile

    return TermProfile()


@pytest.fixture
def profile_with_exempt() -> "TermProfile":
    """含豁免表的术语表：整行原文命中豁免 → detector 判 PASS。

    DECISION(§5.1)：豁免按整词 token 匹配（非短语），故用单 token「Menu」演示
    「译文==原文 且整词命中豁免 → PASS」。
    """
    from pytran.config import TermProfile

    return TermProfile(exempt_english=["Menu"])


# ---------------------------------------------------------------------------
# Windows 沙箱兼容：禁用 pytest 的 0o700 临时目录权限加固。
# TODO(W5): 本补丁因 .pytest_tmp/pip 残留触发（根因已由总控清理 2026-10-09）。
# W5 打包阶段应移除本补丁, 恢复 pytest 默认行为并验证。
#
# 在 Windows 上，os.mkdir(path, 0o700) 会以一个显式 DACL 替换「继承的 ACL」，
# 从而剥除沙箱受限令牌所依赖的 allow-ACE，导致测试进程随后无法 scandir/rmdir
# 该目录（PermissionError: [WinError 5]）。改用宽松 mode 让继承的 ACE 保留，
# 临时目录即可被正常读写。该补丁仅在 Windows 生效，非 Windows 平台保持默认。
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _SAFE_MODE = 0o777

    _orig_mktemp = TempPathFactory.mktemp

    def _mktemp(self, basename: str, numbered: bool = True) -> Path:
        basename = self._ensure_relative_to_basetemp(basename)
        if not numbered:
            p = self.getbasetemp().joinpath(basename)
            p.mkdir(mode=_SAFE_MODE)
        else:
            p = _pytest_pathlib.make_numbered_dir(
                root=self.getbasetemp(), prefix=basename, mode=_SAFE_MODE
            )
            self._trace("mktemp", p)
        return p

    TempPathFactory.mktemp = _mktemp

    _orig_getbasetemp = TempPathFactory.getbasetemp

    def _getbasetemp(self):  # type: ignore[no-untyped-def]
        if self._basetemp is not None:
            return self._basetemp

        if self._given_basetemp is not None:
            basetemp = self._given_basetemp
            if basetemp.exists():
                try:
                    rm_rf(basetemp)
                except OSError as exc:  # pragma: no cover - 沙箱锁定
                    # 给定 basetemp 被沙箱 ACE / 占用目录锁定（如残留的
                    # pip-build-tracker-*），无法清理时退回临时目录，保证
                    # 收集与执行不受影响（不移除沙箱保护性 ACE）。
                    logger.warning(
                        "无法清理给定 basetemp=%s，回退临时目录：%s", basetemp, exc
                    )
                    # 注意：不可使用 tempfile.mkdtemp —— 它会强制 chmod 0o700，
                    # 触发沙箱附加 Deny 扫描 ACE，导致会话收尾 cleanup_dead_symlinks
                    # 再次 PermissionError。改用 os.mkdir(..., 0o777) 保留默认可扫描
                    # ACL。放在系统临时目录下（与手动验证通过的运行方式一致）。
                    import secrets

                    temproot = Path(tempfile.gettempdir())
                    for _ in range(20):
                        cand = temproot / (".pytest_bt_" + secrets.token_hex(4))
                        if not cand.exists():
                            try:
                                os.mkdir(cand, _SAFE_MODE)
                                bt = cand
                                break
                            except FileExistsError:
                                continue
                    else:  # pragma: no cover - 极端冲突
                        bt = Path(tempfile.mkdtemp(prefix=".pytest_bt_"))
                    self._basetemp = Path(bt).resolve()
                    self._trace("fallback basetemp", self._basetemp)
                    return self._basetemp
            basetemp.mkdir(mode=_SAFE_MODE)
            basetemp = basetemp.resolve()
        else:
            from_env = os.environ.get("PYTEST_DEBUG_TEMPROOT")
            temproot = Path(from_env or tempfile.gettempdir()).resolve()
            user = get_user() or "unknown"
            rootdir = temproot.joinpath(f"pytest-of-{user}")
            rootdir.mkdir(mode=_SAFE_MODE, exist_ok=True)
            # 仅类 Unix 需要修复 world-readable 权限；Windows 跳过，
            # 避免 0o700 chmod 剥除继承的沙箱 ACE。
            if os.name == "posix":
                uid = get_user_id()
                if uid is not None:
                    stat_follow = (
                        False if os.stat in os.supports_follow_symlinks else True
                    )
                    rootdir_stat = rootdir.stat(follow_symlinks=stat_follow)
                    if os.stat.S_ISLNK(rootdir_stat.st_mode):
                        raise OSError(
                            f"The temporary directory {rootdir} is a symbolic link. "
                            "Fix this and try again."
                        )
                    if rootdir_stat.st_uid != uid:
                        raise OSError(
                            f"The temporary directory {rootdir} is not owned by the "
                            "current user. Fix this and try again."
                        )
                    if (rootdir_stat.st_mode & 0o077) != 0:
                        chmod_follow = (
                            False if os.chmod in os.supports_follow_symlinks else True
                        )
                        rootdir.chmod(
                            rootdir_stat.st_mode & ~0o077,
                            follow_symlinks=chmod_follow,
                        )
            keep = self._retention_count
            if self._retention_policy == "none":
                keep = 0
            basetemp = make_numbered_dir_with_cleanup(
                prefix="pytest-",
                root=rootdir,
                keep=keep,
                lock_timeout=LOCK_TIMEOUT,
                mode=_SAFE_MODE,
                register=self._exit_stack.callback,
            )
            atexit.register(self._exit_stack.close)
            self._exit_stack.callback(atexit.unregister, self._exit_stack.close)

        assert basetemp is not None, basetemp
        self._basetemp = basetemp
        self._trace("new basetemp", basetemp)
        return basetemp

    TempPathFactory.getbasetemp = _getbasetemp
