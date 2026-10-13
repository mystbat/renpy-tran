@echo off
REM =====================================================================
REM py-tran 3.0 —— 免安装运行脚本（嵌入式 Python 分发）
REM =====================================================================
REM 适用场景：把本目录连同嵌入的 python-3.13.x-embed-amd64 一起发给
REM          不会装 Python 的普通用户，双击即可用。
REM
REM 安全约定（ADR-0004 / §11 / §12）：
REM   * 本目录里【绝不】放 config.ini，也【绝不】包含任何真实 API Key。
REM   * 用户自己的 Key 通过环境变量 TL_API_KEY 注入（见下方「首次运行向导」）。
REM   * 若用户要持久化，应把 config.example.ini 复制为 config.ini 并只填
REM     [Model]/[Game] 等，[API] api_key 必须保持为空。
REM =====================================================================

setlocal
REM 切换到本脚本所在目录（无论从哪里双击）
cd /d "%~dp0"

REM ---- 0. 首次运行：提示设置 API Key（只写环境变量，不落盘 ini） ----
if not defined TL_API_KEY (
    echo ============================================================
    echo  首次运行：请设置你的翻译 API Key（仅本次会话生效）
    echo  想永久生效，请在「系统环境变量」里新建 TL_API_KEY。
    echo ============================================================
    set /p "TL_API_KEY=粘贴你的 Key 后回车（留空则尝试读取 config.ini）: "
)

REM ---- 1. 定位嵌入式 Python ----
if exist "python-3.13.x-embed-amd64\python.exe" (
    set "PY=python-3.13.x-embed-amd64\python.exe"
) else if exist "python\python.exe" (
    set "PY=python\python.exe"
) else (
    REM 回退：用系统 PATH 里的 python
    set "PY=python"
)

REM ---- 2. 确保依赖已装（仅第一次，后续有 site-packages 会跳过） ----
if exist "requirements.txt" (
    "%PY%" -m pip install -r requirements.txt >nul 2>&1
)

REM ---- 3. 把本目录加入 Python 路径并启动 CLI ----
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

if "%1"=="" (
    echo 用法示例（详见 README.md）：
    echo   %~nx0 translate 脚本.rpy --work-dir 工程
    echo   %~nx0 game "游戏目录" --level 1
    echo   %~nx0 migrate "旧2.0目录" --work-dir 新工程
    echo   %~nx0 ui --work-dir 工程
    goto :eof
)

"%PY%" -m pytran.cli %*
endlocal
