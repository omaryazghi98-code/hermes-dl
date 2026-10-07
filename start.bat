@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :fail
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
  echo Installing Chromium for MediaFire browser resolution...
  .venv\Scripts\python.exe -m playwright install chromium
  if errorlevel 1 goto :fail
)
echo.
echo Starting IDM Queue Studio...
start "IDM Queue Studio" http://127.0.0.1:8765
.venv\Scripts\python.exe app.py
exit /b 0
:fail
echo.
echo Setup failed. Review the message above.
pause
exit /b 1
