@echo off
REM ============================================================
REM  AgentVision - run the bridge server WITHOUT the GUI.
REM  Useful for headless / automated setups. The bridge listens
REM  on http://127.0.0.1:7771 and captures the active profile.
REM  Add --profile <name> below to override the active profile.
REM ============================================================
cd /d "%~dp0"
where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")

%PY% python_backend\api\bridge_server.py ^
  --host 127.0.0.1 ^
  --port 7771 ^
  --folder "%~dp0snapshots" ^
  --interval 2

pause
