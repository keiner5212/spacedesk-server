"""
Captura del monitor virtual por los portales XDG: `ScreenCast` con fuente
`VIRTUAL` emparejado con `RemoteDesktop` sobre la MISMA sesion -- el mismo
mecanismo que usa gnome-remote-desktop en GNOME y krfb/KDE Connect en Plasma.

Por que el portal y no la API privada de Mutter que se usaba antes:
  - `org.gnome.Mutter.ScreenCast.RecordVirtual` + `org.gnome.Mutter.RemoteDesktop`
    solo existen si corre Mutter. En KDE Plasma/Wayland no hay ningun nombre
    Mutter en el bus de sesion, asi que TODA conexion de la tablet fallaba con
    `GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown`. El codigo no estaba
    roto: estaba escrito contra un escritorio que ya no es este.
  - `ScreenCast` con `types=VIRTUAL` crea un output virtual REAL de KWin
    (aparece en Ajustes de Pantalla, se le pueden mover ventanas), no una
    superficie de screencast aislada.
  - `RemoteDesktop` sobre la misma sesion inyecta touch/mouse/teclado. Las
    coordenadas van en el espacio logico del stream de PipeWire (`size` de
    `streams[]`), no en el del escritorio combinado -- mismo contrato que con
    Mutter, sin offsets que adivinar.

Flujo: `RemoteDesktop.CreateSession` -> `SelectDevices` ->
`ScreenCast.SelectSources(VIRTUAL)` -> `RemoteDesktop.Start` (dialogo del
portal) -> `streams[]` con el `node_id` de PipeWire -> pipeline GStreamer
(`pipewiresrc path=<node_id>` -> `jpegenc`) -> frames JPEG.

Limitacion conocida de Plasma 6.7: el output virtual se crea a 1920x1080 fijo
(el `video/x-raw` del pipeline lo reescala al tamano configurado), y el
dialogo del portal aparece en cada arranque si el portal no acepta el
`restore_token` (esta en el log cuando pasa).
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

log = logging.getLogger("spacedesk.capture")

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REMOTEDESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"
SESSION_IFACE = "org.freedesktop.portal.Session"

SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4

CURSOR_HIDDEN, CURSOR_EMBEDDED, CURSOR_METADATA = 1, 2, 4

DEVICE_KEYBOARD, DEVICE_POINTER, DEVICE_TOUCHSCREEN = 1, 2, 4
DEVICE_ALL = DEVICE_KEYBOARD | DEVICE_POINTER | DEVICE_TOUCHSCREEN

PERSIST_UNTIL_REVOKED = 2

REQUEST_TIMEOUT = 180.0


class CaptureError(Exception):
    """Fallo al crear o arrancar la captura. El servidor lo reporta una vez."""


class PortalError(Exception):
    pass


def _token_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "spacedesk" / "portal_tokens.json"


class PortalClient:
    """Cliente minimo de los portales XDG sobre una `Gio.DBusConnection`.

    Cada metodo del portal devuelve un request de inmediato y entrega el
    resultado despues en la senal `org.freedesktop.portal.Request.Response`.
    El hilo de trabajo no tiene main context propio, asi que la suscripcion
    queda en el contexto global por defecto -- que es el que ya itera el hilo
    dedicado de `VirtualMonitorCapture`. Correr un segundo main loop sobre el
    mismo contexto hace fallar GLib."""

    def __init__(self, conn):
        self.conn = conn

    def _request(self, iface, method, signature, args, options, timeout=REQUEST_TIMEOUT):
        token = uuid.uuid4().hex
        opts = dict(options)
        opts["handle_token"] = GLib.Variant("s", token)
        sender = self.conn.get_unique_name()[1:].replace(".", "_")
        request_path = f"{PORTAL_OBJECT_PATH}/request/{sender}/{token}"

        outcome: dict = {}
        done = threading.Event()

        def on_signal(_conn, _sender, obj_path, _iface, signal, params):
            if obj_path != request_path:
                return
            if signal == "Response":
                outcome["code"], outcome["results"] = params.unpack()
            elif signal == "Close":
                outcome["error"] = (params.unpack() or ["sesion cerrada por el portal"])[0]
            done.set()

        sub_id = self.conn.signal_subscribe(
            PORTAL_BUS_NAME, REQUEST_IFACE, None, request_path, None,
            Gio.DBusSignalFlags.NONE, on_signal,
        )
        try:
            try:
                self.conn.call_sync(
                    PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, iface, method,
                    # GDBus exige que los parametros sean una tupla.
                    GLib.Variant(f"({signature})", args + (opts,)),
                    GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, -1, None,
                )
            except GLib.Error as exc:
                raise PortalError(f"{iface}.{method}: {exc.message}") from exc
            if not done.wait(timeout):
                outcome.setdefault("error", "timeout esperando al portal")
        finally:
            self.conn.signal_unsubscribe(sub_id)

        if "results" not in outcome:
            raise PortalError(f"{iface}.{method}: {outcome.get('error', 'sin respuesta del portal')}")
        if outcome["code"] != 0:
            raise PortalError(f"{iface}.{method}: {outcome['results'].get('error', 'rechazado')}")
        return outcome["results"]

    def create_remote_desktop_session(self):
        return self._request(
            REMOTEDESKTOP_IFACE, "CreateSession", "a{sv}", (),
            {"session_handle_token": GLib.Variant("s", uuid.uuid4().hex)},
        )["session_handle"]

    def select_devices(self, session, persist, restore_token):
        options = {"types": GLib.Variant("u", DEVICE_ALL)}
        if persist:
            options["persist_mode"] = GLib.Variant("u", PERSIST_UNTIL_REVOKED)
            if restore_token:
                options["restore_token"] = GLib.Variant("s", restore_token)
        self._request(REMOTEDESKTOP_IFACE, "SelectDevices", "oa{sv}", (session,), options)

    def select_sources(self, session, source_type, cursor_mode, restore_token):
        options = {
            "types": GLib.Variant("u", source_type),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", cursor_mode),
        }
        if restore_token and source_type != SOURCE_VIRTUAL:
            # La spec prohibe persistencia en SelectSources cuando la sesion
            # tambien pide input: el restore_token vive en SelectDevices.
            options["restore_token"] = GLib.Variant("s", restore_token)
        self._request(SCREENCAST_IFACE, "SelectSources", "oa{sv}", (session,), options)

    def start_remote_desktop(self, session):
        return self._request(
            REMOTEDESKTOP_IFACE, "Start", "osa{sv}", (session, ""), {},
        )

    def close_session(self, session):
        try:
            self.conn.call_sync(
                PORTAL_BUS_NAME, session, SESSION_IFACE, "Close",
                None, None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.debug("No se pudo cerrar la sesion del portal: %s", exc.message)


class VirtualMonitorCapture:
    """Crea el monitor virtual y entrega frames JPEG via `get_frame()`.

    `width`/`height` son el tamano del framebuffer que ve la tablet;
    `stream_width`/`stream_height` son el tamano logico del stream de PipeWire,
    que es el espacio de coordenadas en el que hay que mandar el input.
    Expone `conn`, `session_handle` y `stream_node_id` para `input.py`."""

    def __init__(
        self,
        width: int,
        height: int,
        jpeg_quality: int = 55,
        source_type: int = SOURCE_VIRTUAL,
        cursor_mode: int = CURSOR_EMBEDDED,
        persist_permissions: bool = True,
        adaptive_quality: bool = True,
        jpeg_quality_min: int = 30,
        jpeg_quality_max: int = 90,
        target_latency: float = 0.25,
    ):
        self.width = width
        self.height = height
        self.jpeg_quality = jpeg_quality
        self.source_type = source_type
        self.cursor_mode = cursor_mode
        self.persist_permissions = persist_permissions

        self._adaptive_quality = adaptive_quality
        self._quality_min = jpeg_quality_min
        self._quality_max = jpeg_quality_max
        self._target_latency = target_latency
        self._latency: float | None = None
        self._last_adjust = 0.0
        self._warned_floor = False
        self._encoder = None

        self.conn = None
        self.session_handle = None
        self.stream_node_id = None
        self.stream_width = width
        self.stream_height = height
        self.granted_devices = 0
        self._pipeline = None
        self._tokens: dict = {}
        self._token_file = _token_path()

        self._frame_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self._last_frame: bytes | None = None
        self._last_frame_lock = threading.Lock()
        self._logged_caps = False

        # pipewiresrc integra PipeWire con el main context de GLib: sin un
        # main loop corriendo, el source nunca despacha y el pipeline queda en
        # PLAYING sin entregar un solo buffer.
        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(
            target=self._loop.run, name="spacedesk-glib", daemon=True,
        )
        self._loop_thread.start()

    # -- ciclo de vida ---------------------------------------------------
    def start(self) -> None:
        Gst.init(None)
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        portal = PortalClient(self.conn)
        self._load_tokens()

        self.session_handle = portal.create_remote_desktop_session()
        log.info("Sesion RemoteDesktop creada: %s", self.session_handle)

        portal.select_devices(
            self.session_handle, self.persist_permissions, self._tokens.get("remote_desktop"),
        )
        portal.select_sources(
            self.session_handle, self.source_type, self.cursor_mode, self._tokens.get("screencast"),
        )

        results = portal.start_remote_desktop(self.session_handle)
        self.granted_devices = results.get("devices", 0)
        log.info(
            "Dispositivos de input otorgados: %s%s",
            self.granted_devices,
            "" if self.granted_devices == DEVICE_ALL else f" (se pidieron {DEVICE_ALL})",
        )

        streams = results.get("streams") or []
        if not streams:
            raise CaptureError("el portal no devolvio ningun stream de video")
        node_id, properties = streams[0]
        self.stream_node_id = node_id
        size = properties.get("size") or (self.width, self.height)
        self.stream_width, self.stream_height = int(size[0]), int(size[1])
        log.info(
            "Stream %s (node_id=%s) logico=%dx%d, fuente=%s",
            properties.get("id", "?"), node_id, self.stream_width, self.stream_height,
            properties.get("source_type", "?"),
        )

        self._save_tokens(results.get("restore_token"))

        # Sin `fd=`: se conecta al daemon de PipeWire del usuario y busca el
        # node por id. La variante con fd heredado del portal (`OpenPipeWireRemote`)
        # aborta el proceso al destruirse el pipeline: `source->loop ==
        # &impl->loop failed at spa/plugins/support/loop.c: remove_from_poll()`.
        # El node lo creamos nosotros en este mismo daemon, asi que es visible.
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc path={node_id} ! "
            # El queue separa captura de codificacion: sin el, capturar,
            # convertir, escalar y comprimir corren en un solo hilo y el frame
            # se atrasesa. leaky=downstream descarta frames viejos en vez de
            # acumularlos cuando el encoder no llega, que es lo que uno quiere
            # en tiempo real.
            f"queue leaky=downstream max-size-buffers=3 ! "
            # El output virtual de Plasma 6.7 sale a 1920x1080 fijo: el
            # videoscale es lo que hace que la tablet reciba exactamente el
            # tamano configurado en config.ini. method=0 es bilineal rapido.
            f"videoconvert ! videoscale method=0 ! "
            f"video/x-raw,width={self.width},height={self.height},format=I420 ! "
            f"jpegenc name=enc quality={self.jpeg_quality} ! "
            f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        self._encoder = self._pipeline.get_by_name("enc")
        self._pipeline.get_bus().add_signal_watch()
        self._pipeline.get_bus().connect("message", self._on_bus_message)
        self._pipeline.set_state(Gst.State.PLAYING)
        log.info("Pipeline de captura iniciado (%dx%d, jpeg=%d)", self.width, self.height, self.jpeg_quality)
        GLib.timeout_add_seconds(15, self._warn_if_no_frames)

    # -- calidad adaptativa ---------------------------------------------
    def note_ack_latency(self, seconds: float) -> None:
        """Latencia medida por el cliente: tiempo entre mandar un frame y que
        llegue su FlowControlAck. Es la unica señal que tenemos del ancho de
        banda real, e incluye la red mas lo que tarda la app."""
        if not self._adaptive_quality or seconds <= 0:
            return
        self._latency = seconds if self._latency is None else self._latency * 0.8 + seconds * 0.2

        now = time.monotonic()
        if now - self._last_adjust < 3.0:
            return
        self._last_adjust = now

        if self._latency > self._target_latency:
            self._apply_quality(self.jpeg_quality - 5)
            if self.jpeg_quality <= self._quality_min and not self._warned_floor:
                self._warned_floor = True
                log.warning(
                    "Latencia alta (%.0f ms) con la calidad JPEG en el minimo (%d). "
                    "Bajar capture.width/height es lo que mas va a ayudar.",
                    self._latency * 1000, self.jpeg_quality,
                )
        elif self._latency < self._target_latency * 0.6:
            self._apply_quality(self.jpeg_quality + 5)

    def _apply_quality(self, quality: int) -> None:
        low = max(1, self._quality_min)
        high = min(100, self._quality_max)
        quality = max(low, min(high, quality))
        if quality == self.jpeg_quality:
            return
        self.jpeg_quality = quality
        if self._encoder is not None:
            self._encoder.set_property("quality", quality)
        log.info("Calidad JPEG -> %d (latencia %.0f ms)", quality, (self._latency or 0) * 1000)

    @property
    def latency(self) -> float | None:
        return self._latency

    def _warn_if_no_frames(self) -> bool:
        with self._last_frame_lock:
            first = self._last_frame is None
        if first:
            log.error(
                "El pipeline arranco pero no llego ningun frame en 15 s. Revisa los "
                "mensajes de error del pipeline de arriba (log_level=DEBUG para el "
                "estado exacto) y que el monitor virtual tenga contenido."
            )
        return False

    def _on_bus_message(self, bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log.error("Error del pipeline de captura: %s (%s)", error, debug)
        elif message.type == Gst.MessageType.EOS:
            log.error("El stream de video termino (EOF); no llegan mas frames")
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if message.src is self._pipeline:
                _old, new, _pending = message.parse_state_changed()
                log.debug("Pipeline -> %s", new.value_nick)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.get_bus().remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        if self.session_handle and self.conn is not None:
            PortalClient(self.conn).close_session(self.session_handle)
            self.session_handle = None
        self._loop.quit()

    # -- frames ---------------------------------------------------------
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
        """Devuelve el JPEG mas reciente. PipeWire solo emite un frame nuevo
        cuando el monitor virtual cambia; si no llega uno dentro de `timeout`,
        se repite el ultimo como keep-alive en vez de bloquear al cliente."""
        try:
            return self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            with self._last_frame_lock:
                return self._last_frame

    # -- permisos persistentes del portal --------------------------------
    def _load_tokens(self) -> None:
        try:
            self._tokens = json.loads(self._token_file.read_text())
        except (OSError, ValueError):
            self._tokens = {}

    def _save_tokens(self, new_token) -> None:
        if not new_token:
            if self.persist_permissions:
                log.info(
                    "El portal no devolvio restore_token: el dialogo de permisos "
                    "va a aparecer en cada arranque"
                )
            return
        self._tokens["remote_desktop"] = new_token
        self._tokens["screencast"] = new_token
        try:
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(json.dumps(self._tokens))
            self._token_file.chmod(0o600)
            log.info("Permisos del portal guardados en %s", self._token_file)
        except OSError as exc:
            log.warning("No se pudo guardar el restore_token: %s", exc)
