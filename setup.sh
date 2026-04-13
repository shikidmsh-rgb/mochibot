#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo ""
echo "  =========================================="
echo "    MochiBot Setup"
echo "  =========================================="
echo ""

# ── Detect Python ──
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "  [ERROR] Python not found."
    echo "  Please install Python 3.11+ from https://www.python.org/downloads/"
    exit 1
fi

# ── Check version >= 3.11 ──
PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "  [ERROR] Python 3.11+ required, found $PY_VERSION"
    exit 1
fi
echo "  [OK] Python $PY_VERSION"

# ── Create venv ──
if [ -d .venv ]; then
    echo "  [OK] Virtual environment already exists, skipping creation."
else
    echo "  Creating virtual environment..."
    $PYTHON -m venv .venv
fi

if [ ! -f .venv/bin/activate ]; then
    echo "  [ERROR] Virtual environment is broken (.venv/bin/activate not found)."
    echo "  Delete the .venv folder and run setup again."
    exit 1
fi
source .venv/bin/activate

# Verify we're in the venv
.venv/bin/python --version >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "  [ERROR] Virtual environment activation failed."
    exit 1
fi

# ── Install dependencies ──
echo "  Installing dependencies..."
.venv/bin/pip install -r requirements.txt fastapi uvicorn sqlite-vec --quiet
echo "  [OK] Dependencies installed."

# ── Launch ──
echo ""
echo "  =========================================="
echo "    Setup complete!"
echo "    Opening admin portal..."
echo "    http://127.0.0.1:8080"
echo "  =========================================="
echo ""
echo "  Configure your API keys and bot token in the browser."
echo "  When done, click \"启动 Bot\" in the admin portal to start the bot."
echo ""

# Open browser (best-effort)
if command -v open &>/dev/null; then
    open http://127.0.0.1:8080 2>/dev/null &
elif command -v xdg-open &>/dev/null; then
    xdg-open http://127.0.0.1:8080 2>/dev/null &
fi

.venv/bin/python -m mochi.admin
