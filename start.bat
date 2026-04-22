@echo off
REM ============================================================
REM  Abide Companion - Windows launcher (zero-config, native Python)
REM  Double-click this file to start the system.
REM ============================================================
REM  First-run UX bar (from the brief):
REM    - no terminal commands
REM    - no Settings changes
REM    - no PATH checkboxes
REM    - no admin / UAC prompts
REM
REM  If Python 3.12+ isn't already on this machine, we download
REM  the official installer from python.org and run it silently
REM  per-user (InstallAllUsers=0, no admin required, no UAC).
REM  The install lands in %LocalAppData%\Programs\Python\Python312\
REM  and we reference python.exe by absolute path — we deliberately
REM  do NOT modify the user's PATH, so we don't pollute any global
REM  environment the user already has.
REM
REM  Why not Docker: WSL2 can't reach DirectShow for PTZ. See
REM  DESIGN-NOTES.md D82. Why silent-install vs winget vs embeddable:
REM  see DESIGN-NOTES.md D100.
REM ============================================================
setlocal enableextensions enabledelayedexpansion

echo.
echo  Abide Companion
echo  =================
echo.

REM --- 1. Move to this script's folder so relative paths work ---
pushd "%~dp0"

REM --- 2. Resolve a usable Python 3.12+ ---
REM    (a) prefer a pre-existing Python on PATH if it's >=3.12
REM    (b) else fall back to a per-user install we shipped earlier
REM    (c) else download + silent-install the official installer
set "PYTHON_EXE="
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
set "BUNDLED_PY=%LocalAppData%\Programs\Python\Python312\python.exe"

where python >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%v in ('python -c "import sys;print(1 if sys.version_info>=(3,12) else 0)" 2^>nul') do set "PY_OK=%%v"
    if "!PY_OK!"=="1" set "PYTHON_EXE=python"
)

if "!PYTHON_EXE!"=="" (
    if exist "!BUNDLED_PY!" set "PYTHON_EXE=!BUNDLED_PY!"
)

if "!PYTHON_EXE!"=="" (
    echo  No Python 3.12+ detected. Installing automatically...
    echo  ^(one-time, ~30 MB download, ~30 s install, no admin required^)
    echo.
    curl --fail --location --silent --show-error -o "%TEMP%\abide-python.exe" "%PYTHON_URL%"
    if errorlevel 1 (
        echo.
        echo  ERROR: could not download Python installer.
        echo  Check your internet connection, then re-run this file.
        echo  Or install Python 3.12 manually from:
        echo      https://www.python.org/downloads/
        pause
        popd
        exit /b 1
    )
    echo  Installing Python 3.12 quietly, per-user...
    "%TEMP%\abide-python.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Include_doc=0
    set "INSTALL_RC=!errorlevel!"
    del "%TEMP%\abide-python.exe" >nul 2>&1
    if not "!INSTALL_RC!"=="0" (
        echo.
        echo  ERROR: Python installer exited with code !INSTALL_RC!.
        echo  Please install Python 3.12 manually from:
        echo      https://www.python.org/downloads/
        pause
        popd
        exit /b 1
    )
    if exist "!BUNDLED_PY!" (
        set "PYTHON_EXE=!BUNDLED_PY!"
    ) else (
        echo.
        echo  ERROR: Python installer completed but python.exe was not
        echo  found at:
        echo      !BUNDLED_PY!
        echo  Please install Python 3.12 manually from:
        echo      https://www.python.org/downloads/
        pause
        popd
        exit /b 1
    )
)

for /f "tokens=2 delims= " %%v in ('"!PYTHON_EXE!" --version 2^>^&1') do set "PY_VER=%%v"
echo  [1/4] Python !PY_VER! ready.

REM --- 3. Create / reuse a local virtualenv so we never touch global Python ---
if not exist ".venv\Scripts\python.exe" (
    echo  [2/4] Creating virtual environment in .venv\ ...
    "!PYTHON_EXE!" -m venv .venv
    if errorlevel 1 (
        echo  ERROR: failed to create virtual environment.
        pause
        popd
        exit /b 1
    )
) else (
    echo  [2/4] Using existing virtual environment.
)

REM --- 4. Install dependencies (quiet on repeat runs) ---
echo  [3/4] Installing / verifying dependencies ^(first run may take a few minutes^)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo  ERROR: pip install failed. See output above.
    pause
    popd
    exit /b 1
)

REM --- 5. Launch uvicorn in a new window so this script can open the browser ---
REM Bind to 127.0.0.1 (loopback only) so the WebSocket + /api/analyze
REM endpoints are not exposed to the LAN. Abide runs on the same
REM machine as the user; loopback is all we need. Switch to 0.0.0.0
REM only if you understand the security trade-off — anyone on the
REM same Wi-Fi could otherwise open a WS to this server and drive the
REM assistant, or use /api/analyze as an anonymous Anthropic proxy.
echo  [4/4] Starting Abide Companion on http://localhost:8000 ...
start "Abide Companion" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

REM --- 6. Give uvicorn a moment to bind the port, then open the browser ---
timeout /t 3 /nobreak >nul
start "" "http://localhost:8000"

echo.
echo  ============================================================
echo   Abide Companion is running in a separate window.
echo.
echo   To STOP: close the "Abide Companion" window, or press
echo           Ctrl+C inside it.
echo  ============================================================
echo.
popd
endlocal
