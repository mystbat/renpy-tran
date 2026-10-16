@echo off
chcp 65001 >nul
setlocal
REM =====================================================================
REM py-tran 3.0 - Ren'Py AI translation tool (embedded Python)
REM Format: UTF-8 (no BOM) + CRLF + chcp 65001. Keep it that way.
REM API key goes to TL_API_KEY env var only (ADR-0004), never to any ini.
REM =====================================================================

cd /d "%~dp0"

REM ---- 1. Locate Python: prefer .venv (all deps incl. PyQt6), then embedded, then PATH ----
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist "python-3.13.x-embed-amd64\python.exe" set "PY=python-3.13.x-embed-amd64\python.exe"
if exist "python-3.13.5-embed-amd64\python.exe" set "PY=python-3.13.5-embed-amd64\python.exe"
if exist "python\python.exe" set "PY=python\python.exe"

REM ---- 2. Ensure deps (FIRST RUN ONLY - marked by .deps_installed) ----
if exist ".deps_installed" goto deps_ok
if exist "requirements.txt" (
    echo First run: installing dependencies, please wait...
    "%PY%" -m pip install -r requirements.txt --quiet
    echo done> ".deps_installed"
)
:deps_ok

REM ---- 3. Add tool dir to PYTHONPATH and launch ----
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

if not "%~1"=="" goto run
REM ---- No arguments: launch GUI (default for double-click users) ----
"%PY%" -m pytran.cli ui --work-dir "%~dp0."
goto :eof

:run
"%PY%" -m pytran.cli %*
endlocal
