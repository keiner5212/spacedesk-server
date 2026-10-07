#!/usr/bin/env bash
# Starts the server. Run ./install.sh once first.
#
#   ./start.sh
#   ./start.sh --config other.ini

set -euo pipefail

cd "$(dirname "$0")"

PYTHON=".venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "No venv found. Run ./install.sh first." >&2
    exit 1
fi

if ! "$PYTHON" -c "import gi, evdev" 2>/dev/null; then
    echo "Missing Python dependencies. Run ./install.sh first." >&2
    exit 1
fi

exec "$PYTHON" main.py "$@"