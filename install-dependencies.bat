@echo off
REM ============================================================
REM  AgentVision - install Python dependencies (Windows)
REM ============================================================
setlocal
cd /d "%~dp0"

where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")

echo Using interpreter: %PY%
%PY% --version
if errorlevel 1 (
  echo.
  echo ERROR: Python was not found on PATH.
  echo Install Python 3.11+ from https://python.org/downloads
  echo and tick "Add python.exe to PATH" in the installer.
  pause
  exit /b 1
)

echo.
echo Upgrading pip...
%PY% -m pip install --upgrade pip

echo.
echo Installing AgentVision requirements...
%PY% -m pip install -r requirements-windows.txt
if errorlevel 1 (
  echo.
  echo ERROR: dependency install failed. See messages above.
  pause
  exit /b 1
)

echo.
echo All dependencies installed. Launch AgentVision with:
echo    "Start AgentVision.bat"
echo.
pause
