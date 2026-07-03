@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "REPO_URL=https://github.com/dhdlrlgns2/TVCF_DOWN_0702.git"
set "APP_DIR=%CD%\TVCF_DOWN_0702"

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

if not exist "tvcf_downloader\updater.py" (
  echo [BOOTSTRAP] App files were not found in this folder.
  echo [BOOTSTRAP] Preparing app from GitHub repository...

  where git >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Git was not found.
    echo Please install Git for Windows, then run START.bat again.
    pause
    exit /b 1
  )

  if not exist "%APP_DIR%\.git" (
    if exist "%APP_DIR%" (
      echo [ERROR] "%APP_DIR%" already exists but is not a git checkout.
      echo Please move START.bat to an empty folder or delete that folder.
      pause
      exit /b 1
    )
    git clone --branch main "%REPO_URL%" "%APP_DIR%"
    if errorlevel 1 (
      echo [ERROR] Failed to download app repository.
      pause
      exit /b 1
    )
  )

  echo [BOOTSTRAP] Starting downloaded app...
  call "%APP_DIR%\START.bat"
  exit /b %ERRORLEVEL%
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
