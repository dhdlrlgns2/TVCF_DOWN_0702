@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "REPO_URL=https://github.com/dhdlrlgns2/TVCF_DOWN_0702.git"
set "APP_DIR=%CD%\TVCF_DOWN_0702"

echo ========================================
echo TVCF Downloader START
echo ========================================

call :detect_python
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
  echo [UPDATE] Update applied. Continuing with updated run script...
)
if not "%UPDATE_CODE%"=="0" if not "%UPDATE_CODE%"=="2" (
  echo [WARNING] Update check failed. Continuing with current files.
)

if not exist "scripts\run_after_update.bat" (
  echo [ERROR] scripts\run_after_update.bat was not found.
  echo Please run git pull or download the latest package again.
  pause
  exit /b 1
)

call "scripts\run_after_update.bat"
exit /b %ERRORLEVEL%

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
