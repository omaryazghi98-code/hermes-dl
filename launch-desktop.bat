@echo off
setlocal
cd /d "%~dp0"

echo.
echo  HERMES MANAGER - Desktop setup
echo  ------------------------------
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher ^(py^) was not found. Install Python 3.11+ and try again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)

echo [2/4] Installing Python dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo [3/4] Checking Node.js and npm...
where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js/npm not found. Install the current Node.js LTS release, then rerun this file.
  pause
  exit /b 1
)

echo [4/4] Installing desktop dependencies if needed...
if not exist "desktop\node_modules\electron\dist\electron.exe" goto install_desktop_deps
if not exist "desktop\node_modules\@ghostery\adblocker-electron\package.json" goto install_desktop_deps
if not exist "desktop\node_modules\cross-fetch\package.json" goto install_desktop_deps
goto start_desktop

:install_desktop_deps
pushd desktop
call npm install
if errorlevel 1 (
  popd
  goto fail
)
popd

:start_desktop
echo Starting Hermes Manager...
pushd desktop
call npm run desktop
set EXITCODE=%ERRORLEVEL%
popd
exit /b %EXITCODE%

:fail
echo.
echo Setup failed. No user files were changed.
pause
exit /b 1
