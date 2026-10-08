@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Hermes] Creating Python environment...
    py -3 -m venv .venv || goto fail

    echo [Hermes] Installing dependencies...
    .venv\Scripts\python.exe -m pip install --upgrade pip || goto fail
    .venv\Scripts\python.exe -m pip install -r requirements.txt || goto fail

)

echo [Hermes] Syncing Python dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto fail

echo [Hermes] Checking Crawl4AI browser dependencies...
.venv\Scripts\crawl4ai-setup.exe || .venv\Scripts\python.exe -m playwright install chromium || goto fail

echo [Hermes] Starting Hermes DL...
echo [Hermes] Browser will open automatically at http://127.0.0.1:8765
.venv\Scripts\python.exe app.py
exit /b

:fail
echo.
echo [Hermes] Setup failed.
pause
