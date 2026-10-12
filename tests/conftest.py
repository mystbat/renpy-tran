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
# Windows 沙箱兼容：禁用 pytest 的 0o700 临时目录权限加固。
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
