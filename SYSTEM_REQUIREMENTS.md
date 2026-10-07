# System requirements

Everything this server needs from the operating system, and how to check that
you have it.

Verified on **Debian sid + KDE Plasma 6.7.4 (Wayland)** and named for
**Ubuntu 24.04 LTS**. The package names are the same on both.

## What you need

| | Requirement |
| --- | --- |
| Desktop | **Wayland** session. KDE Plasma 6+ or GNOME 42+. A plain X11 session has no screen capture portal and will not work. |
| Screen capture | The desktop must have an `xdg-desktop-portal` backend with a **ScreenCast** and **RemoteDesktop** interface. Plasma and GNOME ship one. |
| Video | PipeWire (ships with both desktops) plus the GStreamer PipeWire plugin, which provides the `pipewiresrc` element. |
| Python | 3.10 or newer, with PyGObject and python-evdev from the system. |
| Network | Tablet and PC on the same LAN. Nothing else. |

**Not needed:** kernel modules, `vkms`, `uinput`, a udev rule, or root while
the server runs.

## Packages

Same names on Ubuntu 24.04 and Debian sid:

```bash
sudo apt install -y \
  python3-venv \
  python3-gi \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  libgstreamer1.0-0 \
  libgstreamer-plugins-base1.0-0 \
  gstreamer1.0-plugins-good \
  gstreamer1.0-pipewire \
  python3-evdev
```

Or just run `./install.sh`, which installs these and builds the venv.

What each one is for:

| Package | Why |
| --- | --- |
| `python3-gi`, `gir1.2-gstreamer-1.0` | Talk to D-Bus and GStreamer from Python |
| `libgstreamer1.0-0`, `libgstreamer-plugins-base1.0-0` | GStreamer core, plus `videoscale` and `appsink` |
| `gstreamer1.0-plugins-good` | `videoconvert` and `jpegenc` |
| **`gstreamer1.0-pipewire`** | **`pipewiresrc`, the element that reads the captured stream** |
| `python3-evdev` | Key codes for keyboard and mouse input |
| `python3-venv` | The venv that `install.sh` creates |

`gstreamer1.0-pipewire` is the one that is easy to miss. On some distributions
it is not installed by default, and its absence produces:

```
gst_parse_error: no element "pipewiresrc" (1)
```

Note the name: `gstreamer1.0-pipewire`, **not** `libpipewiregst-0.3-0` and not
`libpipewire-0.3-0` (those are the PipeWire libraries, which do not include the
GStreamer element).

## Desktop setup

Plasma and GNOME already install what they need. Nothing to do.

If you use something else (Sway, Hyprland, river, i3 on X11), you need
`xdg-desktop-portal` plus a backend that implements ScreenCast. Without one you
get no capture at all.

NVIDIA works with the proprietary driver, no extra configuration.

## Check your setup

```bash
./install.sh --check
```

Expected output:

```
OK   venv and Python dependencies
OK   GStreamer elements (pipewiresrc, videoscale, jpegenc)
OK   Wayland session (wayland-0)
OK   screen capture portal (AvailableSourceTypes=7, virtual monitors supported)
```

Check it by hand if you prefer:

```bash
# 1. Am I on Wayland?
echo "$WAYLAND_DISPLAY"

# 2. Does the portal exist, and can it make a virtual monitor?
#    Want 7. 4 alone = virtual monitors only, 1 = physical only.
busctl --user get-property org.freedesktop.portal.Desktop \
  /org/freedesktop/portal/desktop org.freedesktop.portal.ScreenCast \
  AvailableSourceTypes

# 3. Is the GStreamer PipeWire plugin there?
python3 -c "
import gi; gi.require_version('Gst','1.0')
from gi.repository import Gst; Gst.init(None)
print('pipewiresrc:', 'OK' if Gst.ElementFactory.find('pipewiresrc') else 'MISSING')"
```

## Your machine (Debian sid, Plasma 6.7.4)

Everything checked out on this machine except one package:

```
OK   venv and Python dependencies
OK   GStreamer elements (pipewiresrc, videoscale, jpegenc)     <- after installing it
OK   Wayland session (wayland-0)
OK   screen capture portal (AvailableSourceTypes=7, virtual monitors supported)
```

`gstreamer1.0-pipewire` was missing, so the first run reached the permission
dialog, created the virtual monitor and got the PipeWire stream, then died on
`no element "pipewiresrc"`. Installing the package fixed it:

```bash
sudo apt install -y gstreamer1.0-pipewire
```

The portal reports `AvailableSourceTypes=7`, so virtual monitors are supported
here. Plasma 6.7 creates them at a fixed 1920x1080; the server rescales to
whatever `capture.width` and `capture.height` say in `config.ini`.

## If something is wrong

| Message | Cause | Fix |
| --- | --- | --- |
| `no element "pipewiresrc"` | GStreamer PipeWire plugin missing | `sudo apt install gstreamer1.0-pipewire` |
| `ServiceUnknown: org.gnome.Mutter.RemoteDesktop` | Running the old code, written for GNOME only | Update the code. The current version uses the XDG portals. |
| `AvailableSourceTypes` has no `4` | Portal cannot create virtual monitors here | Set `capture.source_type = 1` in `config.ini` to mirror a physical screen |
| Portal call times out | The permission dialog was never answered | Approve it. The server waits 180 s. |
| Black screen, connected | Frame size mismatch, or the virtual monitor was not created | Check the display settings and the log |
| `Display off` forever | Known protocol quirk, fixed in code | Should not happen on this version |