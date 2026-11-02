"""Windows 发布包一键制作脚本（py-tran 3.1 起）。

流程：
1. 清理旧产物 → PyInstaller 按 pytran.spec 构建 dist/pytran/
2. 剪裁误收/用不到的大文件（PruneStep）：
   * opengl32sw.dll（Qt 软件渲染后备，19.7MB，现代 Windows 用不到）
   * Qt6Pdf*.dll（本项目不用 PDF 模块）
   * .qm 翻译文件只保留 zh_* / en_*
   * numpy / pandas 残留（spec excludes 的兜底）
3. 安全检查（红线）：
   * dist/pytran 内不得出现 config.ini 或任何 *.png > 1MB
   * zip 内不得出现 config.ini
4. 压缩为 dist/pytran-<version>-win64.zip（版本号读自 pyproject.toml）

用法：
    .venv/Scripts/python.exe scripts/release_win.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP = DIST / "pytran"

# Qt 软件渲染后备 + PDF 模块：源码不 import，现代 Windows 无需
#
# 进一步瘦身候选（2026-11-02 已核实无人使用，未启用——收益仅 ~3MB 原始，
# 启用前需重新构建并冒烟验证 GUI）：
#     "Qt6Network*.dll",   # 1.7MB，网络走 httpx 不走 Qt
#     "Qt6Svg*.dll",       # 0.6MB，无 svg 图标
#     "qsvg*.dll",         # svg imageformat/iconengine 插件
UNWANTED_DLL = [
    "opengl32sw.dll",
    "Qt6Pdf*.dll",
    "Qt6Quick*",
    "Qt6Test*",
]
# Qt 自带翻译：只留中英
QM_KEEP = re.compile(r"^(zh_|en_|Qt?base_zh|base_en)", re.IGNORECASE)
# spec excludes 的目录兜底
UNWANTED_DIRS = ["numpy", "pandas"]


def build() -> None:
    print("[1/4] PyInstaller 构建 ...")
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(APP, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", "pytran.spec", "--noconfirm", "--clean"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    log = ROOT / "build_exe.log"
    log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        sys.exit(f"PyInstaller 失败（exit={proc.returncode}），日志 {log}")
    if not (APP / "pytran.exe").is_file():
        sys.exit("未找到 dist/pytran/pytran.exe")


def prune() -> None:
    print("[2/4] 剪裁无用大文件 ...")
    saved = 0
    if APP.exists():
        # 整目录剔除
        for d in UNWANTED_DIRS:
            p = APP / "_internal" / d
            if p.exists():
                saved += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
        for f in APP.rglob("*"):
            if not f.is_file():
                continue
            name = f.name.lower()
            if name == "opengl32sw.dll" or any(fmatch(f, p) for p in UNWANTED_DLL):
                saved += f.stat().st_size
                f.unlink()
                continue
            if f.suffix.lower() == ".qm" and not QM_KEEP.match(f.stem):
                saved += f.stat().st_size
                f.unlink()
    print(f"      剪掉 {saved/1e6:.1f} MB")


def fmatch(f: Path, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(f.name.lower(), pattern.lower())


def safety_checks() -> None:
    print("[3/4] 安全检查 ...")
    offenders = []
    for f in APP.rglob("*"):
        if f.is_file():
            if f.name.lower() == "config.ini":
                offenders.append(str(f.relative_to(APP)))
            if f.suffix.lower() == ".png" and f.stat().st_size > 1_000_000:
                offenders.append(str(f.relative_to(APP)) + " (>1MB png)")
    if offenders:
        sys.exit("红线：产物含 [" + ", ".join(offenders) + "]")


def package(version: str) -> Path:
    print(f"[4/4] 打包 zip ...")
    out = DIST / f"pytran-{version}-win64.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(APP.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(APP.parent))
    # zip 内红线复核
    with zipfile.ZipFile(out) as z:
        for n in z.namelist():
            if n.lower().endswith("config.ini"):
                sys.exit(f"红线：zip 内含 {n}")
    return out


def main() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    build()
    prune()
    safety_checks()
    out = package(version)
    mb = out.stat().st_size / 1e6
    print(f"OK → {out.relative_to(ROOT)}  ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
