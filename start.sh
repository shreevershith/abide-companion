#!/usr/bin/env bash
# ============================================================
#  Abide Companion — Mac / Linux launcher
#  Run with:  bash start.sh   (or chmod +x start.sh && ./start.sh)
# ============================================================
set -e

echo
echo " Abide Companion"
echo " ================="
echo

# --- 1. Move to this script's folder so relative paths work ---
cd "$(dirname "$0")"

# --- 2. Verify Docker is running ---
if ! docker info >/dev/null 2>&1; then
    echo " ERROR: Docker does not appear to be running."
    echo
    echo " Please:"
    echo "   1. Open Docker Desktop (Mac) or start the docker daemon (Linux)"
    echo "   2. Wait until it is fully ready"
    echo "   3. Run this script again"
    echo
    exit 1
fi

echo " [1/3] Docker is running."

# --- 3. Build (first run) and start the container ---
echo " [2/3] Starting Abide Companion container..."
echo "       First run may take 3-5 minutes while the image builds."
echo

docker compose up -d --build

# --- 4. Wait a few seconds for uvicorn to bind the port ---
echo " [3/3] Waiting for the server to come up..."
sleep 5

# --- 5. Open the browser ---
URL="http://localhost:8000"
echo
echo " Opening $URL in your browser..."

if command -v open >/dev/null 2>&1; then
    # macOS
    open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
    # Linux (most distros)
    xdg-open "$URL" >/dev/null 2>&1 &
else
    echo " (Could not auto-open — please visit $URL manually)"
fi

echo
echo " ============================================================"
echo "  Abide Companion is running."
echo
echo "  To STOP the system later, run in this folder:"
echo "      docker compose down"
echo
echo "  Or simply quit Docker Desktop."
echo " ============================================================"
echo
