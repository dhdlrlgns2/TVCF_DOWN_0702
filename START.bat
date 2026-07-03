@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo TVCF Downloader START
echo ========================================

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=py -3"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  echo [ERROR] Python was not found.
  echo Please install Python 3.10 or newer, then run START.bat again.
  pause
  exit /b 1
)

echo [STEP 1] Checking app update from git...
%PYTHON_CMD% -m tvcf_downloader.updater
set "UPDATE_CODE=%ERRORLEVEL%"
if "%UPDATE_CODE%"=="2" (
  echo [UPDATE] Restarting START.bat after update...
  call "%~f0"
  exit /b %ERRORLEVEL%
)
if not "%UPDATE_CODE%"=="0" (
  echo [WARNING] Update check failed. Continuing with current files.
)

echo [STEP 2] Checking Python virtual environment...
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

echo [STEP 3] Installing or checking Python packages...
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Failed to install Python packages.
  echo Please check your internet connection and Python installation.
  pause
  exit /b 1
)

echo [STEP 4] Checking Playwright Chromium...
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

echo [STEP 5] Checking bin tools...
"%VENV_PY%" -m tvcf_downloader.environment
if errorlevel 1 (
  echo [ERROR] Required bin tools could not be prepared.
  pause
  exit /b 1
)

echo [STEP 6] Launching GUI...
"%VENV_PY%" main.py
if errorlevel 1 (
  echo [ERROR] The app exited with an error.
  pause
  exit /b 1
)
