@echo off
REM ============================================================
REM  Abide Companion - Windows launcher (native Python)
REM  Double-click this file to start the system.
REM ============================================================
REM  Why not Docker: we need motorized pan/tilt on the Logitech
REM  MeetUp via DirectShow, which Docker Desktop on Windows can't
REM  reach without admin-PowerShell USB/IP setup. Native Python is
REM  a smaller installer and keeps first-run as a double-click.
REM  See DESIGN-NOTES.md D82 for the full rationale.
REM ============================================================
setlocal enableextensions enabledelayedexpansion

echo.
echo  Abide Companion
echo  =================
echo.

REM --- 1. Move to this script's folder so relative paths work ---
pushd "%~dp0"

REM --- 2. Verify Python 3.12+ is on PATH ---
where python >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python is not on PATH.
    echo.
    echo  Please:
    echo    1. Install Python 3.12 or newer from
    echo       https://www.python.org/downloads/
    echo    2. During install, CHECK "Add python.exe to PATH"
    echo    3. Re-run this file.
    echo.
    pause
    popd
    exit /b 1
)

REM Check Python major.minor is >= 3.12
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  [1/4] Python %PY_VER% detected.

REM --- 3. Create / reuse a local virtualenv so we never touch global Python ---
if not exist ".venv\Scripts\python.exe" (
    echo  [2/4] Creating virtual environment in .venv\ ...
    python -m venv .venv
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
