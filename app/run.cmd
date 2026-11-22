@echo off
rem VibeForma launcher.
rem No arguments: start the panel with pythonw.exe (no console window).
rem Any argument: run main.py with python.exe, so stdout, stderr and the exit
rem code reach the caller (--transcribe, --toggle, --cancel, --status,
rem --selftest, --doctor, --help).
setlocal
set "DIR=%~dp0"
set "PY=%DIR%.venv\Scripts\"
set "PYTHONIOENCODING=utf-8"

if "%~1"=="" (
    start "VibeForma" "%PY%pythonw.exe" "%DIR%main.py"
    endlocal
    exit /b 0
)

chcp 65001 >nul
"%PY%python.exe" "%DIR%main.py" %*
set "RC=%errorlevel%"
endlocal & exit /b %RC%
