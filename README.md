# SpaceDesk Linux Server

Use an Android tablet as a second screen for your Linux PC, with the official
spacedesk app, no patching.

## Credits

This project is a fork of
**[leabergero/spacedesk-linux-server](https://github.com/leabergero/spacedesk-linux-server)**
by **Leandro Bergero** (MIT). That project did the hard part: it reverse
engineered the proprietary spacedesk wire protocol from the official app and
built the server that speaks it.

This fork changes three things:

- Capture and input now go through the **XDG desktop portals** instead of
  Mutter's private GNOME D-Bus, so it runs on **KDE Plasma 6** as well as GNOME.
  (The old backend could not work anywhere Mutter was not running.)
- The **USB (Android Open Accessory) transport was removed**. WiFi only, which
  is what most people want and keeps the setup simple.
- Everything adjustable moved to **`config.ini`**, with `install.sh` and
  `start.sh` to set it up and run it.

Full history of what was tried: [ESTADO.md](ESTADO.md).

## Requirements

Debian or Ubuntu with **KDE Plasma 6+ (or GNOME 42+) on Wayland**, Python 3.10+,
and an Android tablet with the official spacedesk app.

Full list, package names per distribution, and how to verify them:
**[SYSTEM_REQUIREMENTS.md](SYSTEM_REQUIREMENTS.md)**.

## Install

```bash
cd linux-server
./install.sh
```

That installs the system packages, creates the venv, and checks that your
desktop can do screen capture. Run `./install.sh --check` any time to see the
status of the prerequisites.

## Run

```bash
./start.sh
```

Then open the spacedesk app on the tablet. It finds the server automatically
over WiFi (UDP broadcast on port 28252), or you can type the PC IP in by hand.

**The first time a client connects**, a KDE permission dialog appears asking to
share the screen and the input device. Approve it. A virtual monitor is created
and shows up in your display settings, so you can drag windows onto it.

## Configure

Everything is in `linux-server/config.ini`.

| Option | Default | What it does |
| --- | --- | --- |
| `server.port` | 28252 | Data and discovery port |
| `server.log_level` | INFO | DEBUG / INFO / WARNING / ERROR |
| `capture.width` / `capture.height` | 1920 / 1200 | Framebuffer size shown on the tablet (1:1, never scaled) |
| `capture.jpeg_quality` | 55 | JPEG quality. 55-75 over WiFi, 90-100 on a fast network |
| `capture.cursor_mode` | 2 | 1 hidden, 2 drawn into the frame, 4 as stream metadata |
| `capture.source_type` | 4 | 4 virtual monitor, 1 mirror a physical screen, 5 ask |
| `capture.persist_permissions` | true | Remember the permission dialog between runs |
| `discovery.enabled` | true | Answer the app's UDP broadcast so it finds the server |

Use a different file: `./start.sh --config my.ini`

## How it works

1. The tablet connects over TCP to port 28252 and sends an `Identification`.
2. The server asks the desktop portal for a screen + input session
   (`ScreenCast` with `types=VIRTUAL`, `RemoteDesktop`).
3. Plasma creates a real virtual monitor and returns a PipeWire stream.
4. GStreamer turns that stream into JPEG frames, sent as `FrameBuffer`
   packets, waiting for a `FlowControlAck` before each one.
5. Touch, mouse and keyboard come back as packets and are replayed into the
   portal session, in the coordinate space of the captured stream.

```
linux-server/
├── install.sh              # install system packages + venv
├── start.sh                # run the server
├── config.ini              # all settings
├── main.py                 # entry point (--config)
└── spacedesk_server/
    ├── config.py           # reads and validates config.ini
    ├── protocol.py         # 128 byte packet header, enums, pack/unpack
    ├── capture.py          # portal screen cast + GStreamer -> JPEG
    ├── input.py            # portal remote desktop input injection
    ├── server.py           # TCP + WebSocket server, per client sender/receiver
    ├── ws_transport.py     # WebSocket framing
    └── discovery.py        # answers the UDP broadcast
```

More detail: [SYSTEM_REQUIREMENTS.md](SYSTEM_REQUIREMENTS.md) for the system
packages and how to verify them, [ESTADO.md](ESTADO.md) for what was tried and
what still needs checking with a real tablet.

## Troubleshooting

**`No se pudo crear la captura: ...`** in the log

Logged once. The server keeps listening and rejects clients with a one line
message instead of a traceback per reconnect. Common causes:

- Not a Wayland session. A plain X11 session has no screen capture portal.
- The portal cannot create a virtual monitor here. Run `./install.sh --check`;
  if `AvailableSourceTypes` has no `4`, set `capture.source_type = 1` to mirror
  a physical screen instead.
- The permission dialog was dismissed or timed out (180 s).

**Black screen but connected**

- Check that the virtual monitor appears in your display settings. If it does
  not, the portal did not create it; read the log.
- Turn on `server.log_level = DEBUG` to see the real frame caps.
- Lower `capture.jpeg_quality` if the image is cut off.

**Touch does nothing**

Look for `Dispositivos de input otorgados:` in the log. It must be `7`
(keyboard 1 + pointer 2 + touchscreen 4). Anything less means the dialog did not
grant everything; accept it again.

**The permission dialog appears every run**

The portal backend did not accept `persist_permissions`. Harmless, just approve
it once more each time.

## References

- Upstream project: [leabergero/spacedesk-linux-server](https://github.com/leabergero/spacedesk-linux-server)
- spacedesk protocol notes: <https://github.com/datronicsoft/SpaceDesk-protocol-documentation>
- [org.freedesktop.portal.ScreenCast](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [org.freedesktop.portal.RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)

## License

MIT, same as the upstream project.