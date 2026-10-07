#!/usr/bin/env bash
# Installs everything the server needs: system packages, the venv, and the
# Python dependencies. Run once, then use ./start.sh.
#
#   ./install.sh
#   ./install.sh --check    # only verify, install nothing

set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PYTHON="$VENV/bin/python"

PACKAGES=(
  python3-venv
  python3-gi
  gir1.2-gstreamer-1.0
  gir1.2-gst-plugins-base1.0
  libgstreamer1.0-0
  libgstreamer-plugins-base1.0-0
  gstreamer1.0-plugins-good
  # Provides the pipewiresrc element. Without it capture fails with
  # `gst_parse_error: no element "pipewiresrc"`.
  gstreamer1.0-pipewire
  python3-evdev
)

install_packages() {
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update && apt-get install -y "${PACKAGES[@]}"
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y "${PACKAGES[@]}"
  else
    echo "Need root to install system packages. Run:"
    echo "  apt-get install -y ${PACKAGES[*]}"
    exit 1
  fi
}

create_venv() {
  if [ ! -x "$PYTHON" ]; then
    echo "Creating $VENV (--system-site-packages)..."
    # --system-site-packages: PyGObject, GStreamer and python-evdev come from
    # the system and cannot be installed with pip.
    /usr/bin/python3 -m venv "$VENV" --system-site-packages
  fi
  "$PYTHON" -m pip install --quiet --upgrade pip
  "$PYTHON" -m pip install --quiet -r requirements.txt
}

check() {
  local ok=0

  if [ -x "$PYTHON" ] && "$PYTHON" -c "import gi, evdev" 2>/dev/null; then
    echo "OK   venv and Python dependencies"
  else
    echo "FAIL venv or Python dependencies missing -- run ./install.sh"
    ok=1
  fi

  if [ -x "$PYTHON" ]; then
    local missing
    missing=$("$PYTHON" -c "
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
print(' '.join(e for e in ('pipewiresrc', 'videoscale', 'jpegenc')
              if Gst.ElementFactory.find(e) is None))
" 2>/dev/null || true)
    if [ -n "$missing" ]; then
      echo "FAIL missing GStreamer elements: $missing"
      echo "     sudo apt install gstreamer1.0-pipewire gstreamer1.0-plugins-good"
      ok=1
    else
      echo "OK   GStreamer elements (pipewiresrc, videoscale, jpegenc)"
    fi
  fi

  if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "FAIL not a Wayland session -- the screen capture portal needs one"
    ok=1
  else
    echo "OK   Wayland session ($WAYLAND_DISPLAY)"
  fi

  local types
  types=$(busctl --user get-property org.freedesktop.portal.Desktop \
    /org/freedesktop/portal/desktop org.freedesktop.portal.ScreenCast \
    AvailableSourceTypes 2>/dev/null | tr -d 'u ' || true)

  if [ -z "$types" ]; then
    echo "FAIL xdg-desktop-portal is not answering -- install xdg-desktop-portal"
    ok=1
  elif [ "$((types & 4))" -eq 0 ]; then
    echo "WARN portal cannot create a virtual monitor (AvailableSourceTypes=$types)."
    echo "     The tablet will mirror a physical screen instead. Set"
    echo "     capture.source_type = 1 in config.ini for that."
  else
    echo "OK   screen capture portal (AvailableSourceTypes=$types, virtual monitors supported)"
  fi

  return $ok
}

if [ "${1:-}" = "--check" ]; then
  check
  exit $?
fi

install_packages
create_venv
check
echo
echo "Done. Start the server with: ./start.sh"