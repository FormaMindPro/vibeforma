@echo off
rem VibeForma launcher: run.cmd [--selftest | --doctor]
setlocal
set "DIR=%~dp0"
set "PY=%DIR%.venv\Scripts\"
set "PYTHONIOENCODING=utf-8"

if /i "%~1"=="--selftest" (
    chcp 65001 >nul
    "%PY%python.exe" "%DIR%main.py" --selftest
    set "RC=%errorlevel%"
    exit /b %RC%
)

if /i "%~1"=="--doctor" (
    chcp 65001 >nul
    "%PY%python.exe" "%DIR%main.py" --doctor
    set "RC=%errorlevel%"
    exit /b %RC%
)

start "VibeForma" "%PY%pythonw.exe" "%DIR%main.py"
endlocal
