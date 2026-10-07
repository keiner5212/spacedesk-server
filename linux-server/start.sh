#!/usr/bin/env bash
# Arranca el servidor spacedesk con el Python del venv del proyecto.
#
# El venv se crea con --system-site-packages porque PyGObject (gi), GStreamer y
# python-evdev vienen del sistema y no se pueden instalar por pip.
#
#   ./start.sh                     -> usa config.ini
#   ./start.sh --config otro.ini   -> otro archivo de configuracion
#   ./start.sh --setup             -> solo prepara el venv y sale

set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PYTHON="$VENV/bin/python"
STAMP="$VENV/.deps-installed"

setup() {
    if [ ! -x "$PYTHON" ]; then
        echo "[spacedesk] creando $VENV (--system-site-packages)..."
        /usr/bin/python3 -m venv "$VENV" --system-site-packages
    fi
    if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
        echo "[spacedesk] instalando dependencias de requirements.txt..."
        "$PYTHON" -m pip install --quiet --upgrade pip
        "$PYTHON" -m pip install --quiet -r requirements.txt
        touch "$STAMP"
    fi
}

setup

if [ "${1:-}" = "--setup" ]; then
    echo "[spacedesk] entorno listo: $PYTHON"
    exit 0
fi

if ! "$PYTHON" -c "import gi, evdev" 2>/dev/null; then
    echo "[spacedesk] ERROR: falta PyGObject (python3-gi) o python3-evdev del sistema." >&2
    echo "[spacedesk]   sudo apt install python3-gi gir1.2-gstreamer-1.0 python3-evdev" >&2
    exit 1
fi

echo "[spacedesk] arrancando (Ctrl+C para detener)..."
exec "$PYTHON" main.py "$@"