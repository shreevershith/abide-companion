#!/usr/bin/env bash
# ============================================================
#  Abide Companion — Mac / Linux launcher (native Python)
#  Run with:  bash start.sh   (or chmod +x start.sh && ./start.sh)
# ============================================================
#  Why not Docker: motorized pan/tilt on Logitech MeetUp needs
#  Windows DirectShow access, which Docker on Windows can't
#  reach without admin-only USB/IP setup (usbipd-win). To keep
#  the launcher a true double-click across platforms, we run
#  the backend natively. See DESIGN-NOTES.md D82 for full
#  rationale. Note: PTZ only activates on Windows + MeetUp;
#  on Mac/Linux the rest of the system works identically.
# ============================================================
set -e

echo
echo " Abide Companion"
echo " ================="
echo

# --- 1. Move to this script's folder so relative paths work ---
cd "$(dirname "$0")"

# --- 2. Verify Python 3.12+ ---
if ! command -v python3 >/dev/null 2>&1; then
    echo " ERROR: python3 is not on PATH."
    echo
    echo " Please install Python 3.12 or newer:"
    echo "   - macOS:  brew install python@3.12   (or https://www.python.org/downloads/)"
    echo "   - Linux:  sudo apt install python3.12 python3.12-venv   (Debian/Ubuntu)"
    echo
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo " [1/4] Python $PY_VER detected."

# --- 3. Create / reuse a local virtualenv so we never touch system Python ---
if [ ! -x ".venv/bin/python" ]; then
    echo " [2/4] Creating virtual environment in .venv/ ..."
    python3 -m venv .venv
else
    echo " [2/4] Using existing virtual environment."
fi

# --- 4. Install dependencies (quiet on repeat runs) ---
echo " [3/4] Installing / verifying dependencies (first run may take a few minutes)..."
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt --quiet

# --- 5. Launch uvicorn in the background so we can also open the browser ---
echo " [4/4] Starting Abide Companion on http://localhost:8000 ..."
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

# --- 6. Wait for port to bind, open browser ---
sleep 3
URL="http://localhost:8000"
if command -v open >/dev/null 2>&1; then
    open "$URL"            # macOS
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &   # Linux
else
    echo " (Could not auto-open — please visit $URL manually)"
fi

echo
echo " ============================================================"
echo "  Abide Companion is running (pid $UVICORN_PID)."
echo
echo "  To STOP: press Ctrl+C in this terminal, or run:"
echo "      kill $UVICORN_PID"
echo " ============================================================"
echo

# Keep the script attached to the uvicorn process so Ctrl+C here stops it cleanly.
wait "$UVICORN_PID"
