@echo off
REM ============================================================
REM  Abide Companion — Windows launcher
REM  Double-click this file to start the system.
REM ============================================================
setlocal enableextensions enabledelayedexpansion

echo.
echo  Abide Companion
echo  =================
echo.

REM --- 1. Move to this script's folder so relative paths work ---
pushd "%~dp0"

REM --- 2. Verify Docker is running ---
docker info >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Docker Desktop does not appear to be running.
    echo.
    echo  Please:
    echo    1. Open Docker Desktop from the Start menu
    echo    2. Wait until the whale icon in the system tray is steady
    echo    3. Run this file again
    echo.
    pause
    popd
    exit /b 1
)

echo  [1/3] Docker is running.

REM --- 3. Build (first run) and start the container ---
echo  [2/3] Starting Abide Companion container...
echo        First run may take 3-5 minutes while the image builds.
echo.

docker compose up -d --build
if errorlevel 1 (
    echo.
    echo  ERROR: Failed to start Abide Companion.
    echo  Check the output above for details.
    echo.
    pause
    popd
    exit /b 1
)

REM --- 4. Wait a few seconds for uvicorn to bind the port ---
echo  [3/3] Waiting for the server to come up...
timeout /t 5 /nobreak >nul

REM --- 5. Open the browser to the app ---
echo.
echo  Opening http://localhost:8000 in your browser...
start "" "http://localhost:8000"

echo.
echo  ============================================================
echo   Abide Companion is running.
echo.
echo   To STOP the system later, open this folder and run:
echo       docker compose down
echo.
echo   Or simply quit Docker Desktop.
echo  ============================================================
echo.
pause
popd
endlocal
