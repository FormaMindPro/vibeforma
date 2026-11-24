@echo off
rem MCP launcher: an AI client starts this file and talks JSON-RPC on stdio.
rem A source checkout keeps it in app\ beside .venv at the root; an installed
rem copy keeps it at the target root beside its own .venv.
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -u -m vibeforma.mcp_server %*
) else if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" -u -m vibeforma.mcp_server %*
) else (
  python -u -m vibeforma.mcp_server %*
)
