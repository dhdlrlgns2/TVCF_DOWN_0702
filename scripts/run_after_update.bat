@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0\.."

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo [STEP 2] Checking Python runtime...
if not defined PYTHON_CMD call :detect_python
if not defined PYTHON_CMD (
  echo [ERROR] Python was not found.
  echo Please install Python 3.10 or newer, then run START.bat again.
  pause
  exit /b 1
)

echo [STEP 3] Checking Python virtual environment...
if not exist ".venv\Scripts\python.exe" (
  echo [SETUP] Creating local Python virtual environment...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
)

set "VENV_PY=%CD%\.venv\Scripts\python.exe"

echo [STEP 4] Installing or checking Python packages...
"%VENV_PY%" -c "from pathlib import Path; import hashlib, sys; req=Path('requirements.txt'); marker=Path('.venv/requirements.sha256'); h=hashlib.sha256(req.read_bytes()).hexdigest(); sys.exit(0 if marker.exists() and marker.read_text(encoding='utf-8').strip()==h else 1)"
if errorlevel 1 (
  echo [SETUP] Python package requirements changed. Installing packages...
  "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Failed to install Python packages.
    echo Please check your internet connection and Python installation.
    pause
    exit /b 1
  )
  "%VENV_PY%" -c "from pathlib import Path; import hashlib; req=Path('requirements.txt'); marker=Path('.venv/requirements.sha256'); marker.write_text(hashlib.sha256(req.read_bytes()).hexdigest(), encoding='utf-8')"
) else (
  echo [CHECK] Python packages are already current.
)

echo [STEP 5] Checking Playwright Chromium...
if not exist ".venv\.playwright_chromium_installed" (
  echo [SETUP] Installing Playwright Chromium browser...
  "%VENV_PY%" -m playwright install chromium
  if errorlevel 1 (
    echo [WARNING] Playwright Chromium install failed.
    echo The app will start, but Playwright fallback may not work.
  ) else (
    echo installed>".venv\.playwright_chromium_installed"
  )
)

echo [STEP 6] Checking bin tools...
"%VENV_PY%" -m tvcf_downloader.environment
if errorlevel 1 (
  echo [ERROR] Required bin tools could not be prepared.
  pause
  exit /b 1
)

echo [STEP 7] Launching GUI...
"%VENV_PY%" main.py
if errorlevel 1 (
  echo [ERROR] The app exited with an error.
  pause
  exit /b 1
)

exit /b 0

:detect_python
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=py -3"
  goto :eof
)
where python >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"
goto :eof
