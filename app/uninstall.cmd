@echo off
rem VibeForma uninstaller.
rem Runs uninstall.py with a Python installed outside the install folder when
rem one is there: Windows keeps .venv\Scripts\python.exe locked while it runs,
rem and .venv then cannot be removed. The venv python is the last resort.
setlocal
set "DIR=%~dp0"
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  set "PY="%DIR%.venv\Scripts\python.exe""
)
%PY% "%DIR%uninstall.py" %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
