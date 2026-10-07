@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || goto fail
  .venv\Scripts\python.exe -m pip install --upgrade pip || goto fail
  .venv\Scripts\python.exe -m pip install -r requirements.txt || goto fail
  .venv\Scripts\python.exe -m playwright install chromium || goto fail
)
start "Hermes DL" http://127.0.0.1:8765
.venv\Scripts\python.exe app.py
exit /b
:fail
echo Setup failed.
pause
