# Status

Short version of where this project stands. See [README.md](README.md) for how
to install and use it.

## Works

- Tablet connects over WiFi and is discovered automatically (UDP 28252).
- Protocol handshake, `FrameBuffer` / `FlowControlAck` loop, JPEG video.
- Touch, mouse and keyboard, injected into the virtual monitor over the portal.
- Virtual monitor created through the XDG portals, so it works on KDE Plasma 6
  and GNOME 42+.

## Pending (needs the real tablet)

1. That the virtual monitor shows up in the display settings.
2. That `capture.persist_permissions` actually suppresses the permission dialog
   on the second run. `xdg-desktop-portal-kde` has a history of implementing
   this only halfway; it is cosmetic if it does not work.
3. That `Dispositivos de input otorgados:` reports 7 (keyboard + pointer +
   touchscreen).
4. Touch landing on the virtual monitor and not on the wrong screen.

## Why the capture backend changed (2026-10-06)

The old `capture.py` and `input.py` only spoke Mutter's private GNOME D-Bus
(`org.gnome.Mutter.ScreenCast.RecordVirtual` plus
`org.gnome.Mutter.RemoteDesktop`). The machine this was developed on is now
Debian sid with **KDE Plasma 6.7.4 on Wayland**, and Mutter is not installed, so
those bus names do not exist and every connection from the tablet failed with:

```
GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown:
The name org.gnome.Mutter.RemoteDesktop was not provided by any .service files
```

The code was not broken, it was written for a desktop that was no longer being
used. It now uses `org.freedesktop.portal.ScreenCast` with `types=VIRTUAL` plus
`org.freedesktop.portal.RemoteDesktop` on the same session. On Plasma 6.7
`AvailableSourceTypes` is 7 (monitor, window, virtual), so the portal can create
a real, placeable KWin output rather than an isolated screencast surface.

The USB (Android Open Accessory) transport was removed at the same time. It
worked, but it needed a udev rule, pyusb and a separate venv, and it is not how
most people want to run this.

## Findings worth keeping

Things that were expensive to learn and are still true. The ones marked
CONFIRMED come from decompiling the official 4.8 APK with jadx, not from
guessing.

- **The tablet sends touches as PEN (13), not TOUCH (12).** CONFIRMED. The app
  builds them in `C2641V1.m13082a()`/`m13083b()` straight from the touchscreen
  `MotionEvent`. This server used to drop every one of them on the floor, which
  is why nothing responded to touch. `protocol.PenPacket` + `input.handle_pen`
  now handle them.
- **The mouse `button_flags` at offset 20 is not a button mask.** CONFIRMED. It
  is the state of a modifier key the app intercepts: `m12852o1(false)` from
  `onKeyDown` becomes `8`, `m12852o1(true)` from `onKeyUp` becomes `16`
  (`C2641V1.m13084c`). The old "any non-zero means left button" heuristic was
  firing clicks on key release.
- **Discovery is a 308 byte binary struct, not a text echo.** CONFIRMED. The app
  receives with a buffer of exactly 308 (`C2547F2` calls `m13256c(308)`) and
  parses it as `C2674f`: name in UTF-16LE at offset 0, IPv4 at 256, port at 260,
  OS type at 264, capability flags at 280, all little-endian. Bit 16 of the
  flags means "this server speaks TLS", which we must not set. Answering with
  the `SPACEDESK-NET-CLIENT` magic, as the upstream project did, could never
  work: the 20 byte reply is parsed as a 308 byte buffer, the name comes out
  as garbage and the parse throws. See `protocol.build_discovery_response`.
- **The app's drawing surface is always 1920x1200.** Its `Identification` packet
  says something else depending on the transport. Confirmed with
  `adb logcat`: `addSurfaceChangedCallback ... 0,0-1920,1200`. The framebuffer
  size is a config value, not something read from the client.
- **The app does not scale.** It shows the framebuffer 1:1, so a frame smaller
  than the screen shows up as a small rectangle in a corner, not as a scaled
  image.
- **`cursor_mode` 2, not 1.** 2 is "embedded" (the cursor is drawn into the
  pixels) in the XDG portal convention. 1 is hidden. Confusing the two is why
  the cursor was invisible in an earlier attempt.
- **H264 does not work with this tablet.** H264 was tried three times at
  different resolutions and profiles, and produced a permanently black screen.
  The tablet's hardware decoder rejects the stream. Do not spend time on it
  without first capturing the real traffic from the official Windows server.
  MJPEG works.
- **Plasma 6.7 creates the virtual output at a fixed 1920x1080.** It is
  hardcoded inside the portal backend. The GStreamer pipeline rescales to the
  configured framebuffer size, so the tablet still gets 1920x1200. Input
  coordinates are scaled to the stream's logical size (1920x1080), not to the
  framebuffer size.
- **Input coordinates go in the stream's coordinate space**, not the combined
  desktop's. The portal maps them to the right output, so there is no offset to
  guess. This is the reason the portal is used instead of evdev/uinput.

## Previous attempts, for the record

- **vkms (kernel virtual KMS driver).** Mutter enumerated it but never treated
  it as part of the normal interactive desktop: the mouse could not enter it and
  keyboard shortcuts could not reach it. Verified with the real user, not just
  on paper.
- **Mutter `RecordVirtual` without `is-platform`.** Produced a screen you could
  look at but not interact with, which defeats the purpose.
- **Mapping touch to the monitor via `org.gnome.desktop.peripherals.touchscreen`
  or by temporarily making the virtual monitor primary.** Abandoned as too
  intrusive for the benefit. Not needed with the portal backend.