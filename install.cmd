@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python not found. Install Python 3.10+ from python.org and run install.cmd again.
    pause
    exit /b 1
)
python install.py %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
