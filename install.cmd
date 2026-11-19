@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python не найден. Установите Python 3.10+ с python.org и запустите install.cmd снова.
    pause
    exit /b 1
)
python install.py %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
