#!/usr/bin/env bash
# ============================================================
#  Abide Companion — macOS launcher (zero-config, native Python)
#  Double-click this file in Finder. On first run macOS may say
#  "cannot be opened because it is from an unidentified developer"
#  — right-click this file once and choose Open, then click Open
#  on the confirmation dialog. After that, every future launch is
#  silent and a normal double-click works.
#
#  (File extension is .command, not .sh, so Finder knows to run
#  this in Terminal on double-click. Linux users: use start.sh.)
# ============================================================
#  First-run UX bar (from the brief):
#    - no terminal commands
#    - no manual dependency install
#    - no Settings changes beyond the one-time Gatekeeper bypass
#      above, which Apple requires for any unsigned app
#
#  If Python 3.12+ is missing on macOS, we download the official
#  Apple Installer .pkg from python.org and open it via the
#  Installer GUI. The user clicks Continue a few times and enters
#  their admin password once; the installer handles everything
#  else. We then continue automatically.
#
#  Why not Docker: WSL2 can't reach DirectShow for PTZ on Windows,
#  so we dropped Docker for cross-platform consistency. See
#  DESIGN-NOTES.md D82. Why this specific install flow: D100.
# ============================================================
set -e

echo
echo " Abide Companion"
echo " ================="
echo

# --- 1. Move to this script's folder so relative paths work ---
cd "$(dirname "$0")"

# --- 2. Resolve a usable Python 3.12+ ---
have_python_312() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null
}

PYTHON_BIN=""
if have_python_312; then
    PYTHON_BIN="python3"
else
    # macOS — download the official .pkg and open the GUI installer.
    # One admin-password prompt; no terminal config.
    PKG_URL="https://www.python.org/ftp/python/3.12.7/python-3.12.7-macos11.pkg"
    PKG_PATH="/tmp/abide-python-3.12.7.pkg"
    echo " No Python 3.12+ detected. Downloading the official installer..."
    echo " (one-time, ~45 MB; the Apple Installer will open when ready)"
    echo
    if ! curl --fail --location --silent --show-error -o "$PKG_PATH" "$PKG_URL"; then
        echo
        echo " ERROR: could not download Python installer."
        echo " Check your internet connection, then re-run this file."
        echo " Or install Python 3.12 manually from:"
        echo "     https://www.python.org/downloads/"
        exit 1
    fi
    echo " Opening installer — please click through and enter your admin password."
    # -W waits for the installer to close before we continue
    open -W "$PKG_PATH"
    rm -f "$PKG_PATH"
    if have_python_312; then
        PYTHON_BIN="python3"
    else
        echo
        echo " ERROR: Python 3.12 install was not detected after the installer closed."
        echo " Please install Python 3.12 manually from:"
        echo "     https://www.python.org/downloads/"
        exit 1
    fi
fi

PY_VER=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
echo " [1/4] Python $PY_VER ready."

# --- 3. Create / reuse a local virtualenv so we never touch system Python ---
if [ ! -x ".venv/bin/python" ]; then
    echo " [2/4] Creating virtual environment in .venv/ ..."
    "$PYTHON_BIN" -m venv .venv
else
    echo " [2/4] Using existing virtual environment."
fi

# --- 4. Install dependencies (quiet on repeat runs) ---
echo " [3/4] Installing / verifying dependencies (first run may take a few minutes)..."
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt --quiet

# --- 5. Launch uvicorn in the background so we can also open the browser ---
# Bind to 127.0.0.1 (loopback only) so the WebSocket + /api/analyze
# endpoints are not exposed to the LAN. Abide runs on the same machine
# as the user; loopback is all we need. Switch to 0.0.0.0 only if you
# understand the security trade-off — anyone on the same Wi-Fi could
# otherwise open a WS to this server and drive the assistant, or use
# /api/analyze as an anonymous Anthropic proxy.
echo " [4/4] Starting Abide Companion on http://localhost:8000 ..."
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
UVICORN_PID=$!

# --- 6. Wait for port to bind, open browser ---
sleep 3
open "http://localhost:8000"

echo
echo " ============================================================"
echo "  Abide Companion is running (pid $UVICORN_PID)."
echo
echo "  To STOP: press Ctrl+C in this terminal window, or close it."
echo " ============================================================"
echo

# Keep the script attached to the uvicorn process so Ctrl+C here stops it cleanly.
wait "$UVICORN_PID"
