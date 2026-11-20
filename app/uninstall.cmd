@echo off
setlocal
set "DIR=%~dp0"
set "PY=%DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%DIR%uninstall.py" %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
