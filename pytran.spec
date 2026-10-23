# -*- mode: python ; coding: utf-8 -*-
#
# py-tran 3.0 —— PyInstaller 打包规格（--onedir 单目录分发）
#
# 安全红线（ADR-0004 / §11 / §12）：本 spec 明确【排除】config.ini 与任何
# 真实密钥；打包产物不含用户的 Key，运行期通过环境变量 TL_API_KEY 注入。
# 打包命令：
#   pyinstaller pytran.spec
# 产物在 dist/pytran/，内含 pytran.exe 与依赖；双击运行等同于 CLI。
#
# 注意：archive/、*.rpy、*.rpyc、*.rpa、config.ini 等被 .gitignore 排除，
# 本 spec 的 exclude 只是二次保险，确保即便误带也不会进包。

import os

block_cipher = None

# 需要显式随包的数据（examples / 文档 / 图标），不含任何真实游戏或密钥
datas = [
    ("config.example.ini", "."),
    ("README.md", "."),
    ("requirements.txt", "."),
    ("assets", "assets"),
]

# 二次保险：绝不把 config.ini / 真实数据打进包
excluded_imports = []

a = Analysis(
    ["pytran\\cli.py"],
    pathex=[os.getcwd()],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "pytran.parser",
        "pytran.project",
        "pytran.migrate",
        "pytran.exporter",
        "pytran.engine",
        "pytran.providers",
        "pytran.unren",
        "pytran.validators",
        "pytran.journal",
        "pytran.ui_state",
        "pytran.ui",
        "pytran.backfill",
        "pytran.config",
        "pytran.models",
        "pytran.interfaces",
        "openai",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除一切可能携带密钥/真实数据的模式（保险）
        "config.ini",
        "*.rpy",
        "*.rpyc",
        "*.rpa",
        "archive",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pytran",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/pytran.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="pytran",
)
