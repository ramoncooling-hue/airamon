@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8899"
set "CYBERPAL_DATA_DIR=%CD%\data"
set "URL=http://127.0.0.1:%PORT%/"

where python >nul 2>nul
if errorlevel 1 (
  echo CyberPal requires Python 3.11 or newer.
  pause
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo CyberPal requires Python 3.11 or newer.
  pause
  exit /b 1
)

curl --silent --fail --max-time 2 "%URL%api/health" >nul 2>nul
if not errorlevel 1 (
  start "" "%URL%"
  exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating virtual environment and installing dependencies...
  python -m venv .venv
  if errorlevel 1 goto :fail
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 goto :fail
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
)

start "CyberPal" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%"
timeout /t 2 /nobreak >nul
start "" "%URL%"
exit /b 0

:fail
echo CyberPal setup failed. Check Python and network access, then retry.
pause
exit /b 1
