@echo off
REM ============================================================
REM  AgentVision - launch the control-panel GUI (Windows)
REM ============================================================
cd /d "%~dp0"
where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")

%PY% python_backend\gui\agent_vision_gui.py
if errorlevel 1 (
  echo.
  echo AgentVision exited with an error. If this is the first run,
  echo make sure you ran "install-dependencies.bat" first.
  pause
)
