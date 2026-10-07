"""
Captura de pantalla vía `org.gnome.Mutter.ScreenCast.RecordVirtual`, pareada
con una sesión `org.gnome.Mutter.RemoteDesktop` -- el mismo mecanismo que usa
gnome-remote-desktop para sus sesiones RDP/VNC (ver `grd-session.c` del
proyecto real).

Por qué este pareo y no vkms ni un RecordVirtual "suelto":
  - `RecordVirtual` con la propiedad `is-platform: true` le dice a Mutter que
    trate el monitor virtual como uno real (no como una sesión de pantalla
    compartida aislada), para que GNOME extienda el escritorio normalmente
    sobre él (fondo, ventanas).
  - Vincular la sesión de ScreenCast a una de RemoteDesktop
    (propiedad `remote-desktop-session-id`) habilita los métodos
    `Notify{Touch,Pointer,Keyboard}*` de esa sesión de RemoteDesktop, que
    inyectan input DIRECTAMENTE en el stream correcto: las coordenadas de
    `NotifyTouchDown`/`NotifyPointerMotionAbsolute` son relativas al stream
    (0..width, 0..height) -- Mutter les suma el offset real internamente
    (confirmado leyendo `meta_stream_virtual_transform_position` en el fuente
    de mutter: `*x = stream_x + view_layout.x`). A diferencia de evdev/uinput,
    no hay ambigüedad de a qué monitor pertenecen ni offset que adivinar.
  - Se descartó vkms (intento anterior, ver ESTADO.md/memoria del proyecto):
    aun configurado y posicionado desde Ajustes de Pantalla de forma
    persistente (no solo vía D-Bus), Mutter nunca lo incluía en el área
    interactiva normal -- ni el mouse real podía entrar, ni el atajo
    "mover ventana al monitor de la derecha" lo alcanzaba. Confirmado
    empíricamente con el usuario real, no solo por teoría.

Flujo: `RemoteDesktop.CreateSession()` -> `SessionId` ->
`ScreenCast.CreateSession({"remote-desktop-session-id": SessionId})` ->
`Session.RecordVirtual({"is-platform": true, "cursor-mode": 1})` ->
`stream_path` -> suscribirse a `PipeWireStreamAdded` en el stream ->
`RemoteDesktop Session.Start()` -> recibir `node_id` -> pipeline GStreamer a
JPEG.

Nota: se probo H264 (x264enc, con un segundo pipeline de codificacion
on-demand para no romper la cadena de referencias inter-frame) tres veces:
con 1920x1200 (rechazado por el hardware decoder de la tablet), sin el NAL
de AUD (mismo error), y forzando 1920x1080 + profile baseline + level 4.0
(la combinacion mas universalmente compatible segun el foro oficial de
spacedesk, que reporta el mismo sintoma con 1200 en otros usuarios) --
ninguna funciono, pantalla negra persistente. Se revirtio a MJPEG
definitivamente. Si se retoma, hace falta capturar el trafico real del
servidor Windows oficial (Wireshark) o decompilar mas a fondo la
configuracion del MediaCodec en el APK -- adivinar contra el decoder real
ya se probo agotado.
"""

import logging
import os
import queue
import threading

os.environ.setdefault("GST_PLUGIN_PATH", "/usr/lib/x86_64-linux-gnu/gstreamer-1.0")

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

log = logging.getLogger("spacedesk.capture")

SCREENCAST_BUS_NAME = "org.gnome.Mutter.ScreenCast"
SCREENCAST_OBJECT_PATH = "/org/gnome/Mutter/ScreenCast"
SCREENCAST_SESSION_IFACE = "org.gnome.Mutter.ScreenCast.Session"
SCREENCAST_STREAM_IFACE = "org.gnome.Mutter.ScreenCast.Stream"

REMOTEDESKTOP_BUS_NAME = "org.gnome.Mutter.RemoteDesktop"
REMOTEDESKTOP_OBJECT_PATH = "/org/gnome/Mutter/RemoteDesktop"
REMOTEDESKTOP_SESSION_IFACE = "org.gnome.Mutter.RemoteDesktop.Session"


class VirtualMonitorCapture:
    """Crea la sesión RemoteDesktop+ScreenCast de `width`x`height` y entrega
    frames JPEG codificados a través de `get_frame()` (bloqueante, devuelve el
    frame más reciente). Expone `conn`, `remote_desktop_session_path` y
    `stream_path` para que `input.py` pueda inyectar input en el mismo
    stream."""

    def __init__(self, width: int, height: int, jpeg_quality: int = 55):
        self.width = width
        self.height = height
        self.jpeg_quality = jpeg_quality

        self._frame_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self._last_frame: bytes | None = None
        self._last_frame_lock = threading.Lock()
        self._logged_caps = False
        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(target=self._loop.run, daemon=True)
        self._loop_thread.start()

        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._screencast_session_path = None
        self.remote_desktop_session_path = None
        self.stream_path = None
        self._pipeline = None

    def start(self) -> None:
        Gst.init(None)

        self.remote_desktop_session_path = self._call(
            REMOTEDESKTOP_OBJECT_PATH, "org.gnome.Mutter.RemoteDesktop", "CreateSession",
            None, "(o)", bus_name=REMOTEDESKTOP_BUS_NAME,
        )[0]
        session_id = self._get_property(
            self.remote_desktop_session_path, REMOTEDESKTOP_SESSION_IFACE, "SessionId",
            bus_name=REMOTEDESKTOP_BUS_NAME,
        )
        log.info("Sesion RemoteDesktop creada: %s (id=%s)", self.remote_desktop_session_path, session_id)

        screencast_props = {"remote-desktop-session-id": GLib.Variant("s", session_id)}
        self._screencast_session_path = self._call(
            SCREENCAST_OBJECT_PATH, "org.gnome.Mutter.ScreenCast", "CreateSession",
            GLib.Variant("(a{sv})", (screencast_props,)), "(o)", bus_name=SCREENCAST_BUS_NAME,
        )[0]
        log.info("Sesion ScreenCast creada (vinculada): %s", self._screencast_session_path)

        record_props = {
            # cursor-mode=1 (EMBEDDED, ver org.gnome.Mutter.ScreenCast.xml):
            # dibujar el cursor del sistema directo en los pixeles del frame.
            # OJO: el valor 2 es "metadata" (cursor aparte, el cliente tiene
            # que leerla y dibujarla el mismo) -- confundir 1 con 2 fue el bug
            # por el que el cursor nunca se veia, ni en este intento ni en el
            # original con RecordVirtual.
            "cursor-mode": GLib.Variant("u", 1),
            # is-platform=true: tratar este monitor como uno real (GNOME lo
            # extiende normalmente: fondo, ventanas), no como una sesion de
            # pantalla compartida aislada.
            "is-platform": GLib.Variant("b", True),
            # Sin "modes" el stream queda redimensionable y mutter elige el
            # tamaño -- forzamos un único modo fijo con la resolución exacta
            # que pidio el cliente (ver server.py: la app no escala el
            # framebuffer, lo muestra a tamaño real).
            "modes": GLib.Variant("aa{sv}", [{
                "size": GLib.Variant("(uu)", (self.width, self.height)),
                "is-preferred": GLib.Variant("b", True),
            }]),
        }
        self.stream_path = self._call(
            self._screencast_session_path, SCREENCAST_SESSION_IFACE, "RecordVirtual",
            GLib.Variant("(a{sv})", (record_props,)), "(o)", bus_name=SCREENCAST_BUS_NAME,
        )[0]
        log.info("Monitor virtual grabado, stream: %s", self.stream_path)

        node_event = threading.Event()
        node_holder = {}

        def on_signal(connection, sender, obj_path, iface, signal, params):
            if signal == "PipeWireStreamAdded":
                node_holder["node_id"] = params.unpack()[0]
                node_event.set()

        self.conn.signal_subscribe(
            None, SCREENCAST_STREAM_IFACE, None, self.stream_path, None,
            Gio.DBusSignalFlags.NONE, on_signal,
        )

        # RemoteDesktopSession.Start() arranca tambien el/los streams de
        # ScreenCast vinculados a esta sesion -- llamar Stream.Start() aparte
        # tira "Stream already started" (confirmado empiricamente).
        self._call(self.remote_desktop_session_path, REMOTEDESKTOP_SESSION_IFACE, "Start",
                   None, None, bus_name=REMOTEDESKTOP_BUS_NAME)

        if not node_event.wait(10):
            raise RuntimeError("timeout esperando PipeWireStreamAdded de Mutter")
        node_id = node_holder["node_id"]
        log.info("PipeWire node_id=%s", node_id)

        try:
            stream_params = self._get_property(
                self.stream_path, SCREENCAST_STREAM_IFACE, "Parameters", bus_name=SCREENCAST_BUS_NAME,
            )
            log.info(
                "Pedido %dx%d -- Mutter negocio stream Parameters: %s",
                self.width, self.height, stream_params,
            )
        except GLib.Error:
            log.exception("No se pudo leer Stream.Parameters para diagnostico")

        self._pipeline = Gst.parse_launch(
            f"pipewiresrc path={node_id} ! "
            # Sin width/height aca, PipeWire negocia su tamaño por defecto
            # (confirmado empiricamente: 1280x720) en vez del tamaño real del
            # monitor virtual que configuramos en RecordVirtual -- forzar el
            # caps filter justo despues de pipewiresrc es lo que hace que la
            # negociacion SPA elija el modo que pedimos.
            f"video/x-raw,width={self.width},height={self.height} ! "
            f"videoconvert ! "
            f"video/x-raw,format=I420 ! "
            f"jpegenc quality={self.jpeg_quality} ! "
            f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        log.info("Pipeline de captura iniciado")

    def _on_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if not self._logged_caps:
            self._logged_caps = True
            log.info("Caps reales del primer frame capturado: %s", sample.get_caps().to_string())
        buf = sample.get_buffer()
        success, mapinfo = buf.map(Gst.MapFlags.READ)
        if success:
            jpeg_bytes = bytes(mapinfo.data)
            buf.unmap(mapinfo)
            with self._last_frame_lock:
                self._last_frame = jpeg_bytes
            if self._frame_queue.full():
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    pass
            self._frame_queue.put(jpeg_bytes)
        return Gst.FlowReturn.OK

    def get_frame(self, timeout: float = 1.0) -> bytes | None:
        """Devuelve el JPEG más reciente. Mutter/PipeWire solo emite un frame
        nuevo cuando hay cambios reales en el monitor virtual (sin 'daño' en
        pantalla no hay sample) -- si no llega uno nuevo dentro de `timeout`,
        se repite el último conocido como keep-alive en vez de bloquear
        indefinidamente al cliente esperando un frame que puede no llegar nunca."""
        try:
            return self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            with self._last_frame_lock:
                return self._last_frame

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        if self.stream_path is not None:
            try:
                self._call(self.stream_path, SCREENCAST_STREAM_IFACE, "Stop",
                           None, None, bus_name=SCREENCAST_BUS_NAME)
            except GLib.Error:
                pass
        if self._screencast_session_path is not None:
            try:
                self._call(self._screencast_session_path, SCREENCAST_SESSION_IFACE, "Stop",
                           None, None, bus_name=SCREENCAST_BUS_NAME)
            except GLib.Error:
                pass
        if self.remote_desktop_session_path is not None:
            try:
                self._call(self.remote_desktop_session_path, REMOTEDESKTOP_SESSION_IFACE, "Stop",
                           None, None, bus_name=REMOTEDESKTOP_BUS_NAME)
            except GLib.Error:
                pass
        self._loop.quit()

    def _call(self, obj_path, iface, method, arg_variant, return_type, bus_name):
        return_variant_type = GLib.VariantType.new(return_type) if return_type else None
        result = self.conn.call_sync(
            bus_name, obj_path, iface, method, arg_variant,
            return_variant_type, Gio.DBusCallFlags.NONE, -1, None,
        )
        return result.unpack() if result else None

    def _get_property(self, obj_path, iface, prop_name, bus_name):
        result = self.conn.call_sync(
            bus_name, obj_path, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (iface, prop_name)),
            GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, -1, None,
        )
        return result.unpack()[0]
