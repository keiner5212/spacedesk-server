"""
Servidor principal: acepta conexiones en el puerto 28252 (TCP crudo para la app
nativa, WebSocket para el visor HTML5 -- ver memoria del proyecto sobre por qué
hay que soportar ambos transportes), hace el handshake Identification, y por
cada cliente conectado corre dos tareas concurrentes:
  - sender: toma frames JPEG de la captura compartida y los manda como paquetes
    FrameBuffer, respetando el control de flujo (espera FlowControlAck antes de
    mandar el siguiente -- ver protocol.py / memoria sobre t2.java).
  - receiver: lee paquetes entrantes (Touch/Mouse/Keyboard/FlowControlAck/Disconnect)
    y los despacha.

Una sola instancia de captura de pantalla se comparte entre todos los clientes
conectados (es "la" pantalla extendida del PC, no una por cliente).
"""

import asyncio
import collections
import logging
import socket
import time

from . import protocol as proto
from . import ws_transport
from .capture import VirtualMonitorCapture
from .config import Settings
from .discovery import start_discovery_responder
from .input import VirtualInput

log = logging.getLogger("spacedesk.server")


class PeekedReader:
    """Envuelve un StreamReader para poder 'devolver' bytes ya leídos durante
    la detección del transporte (peek de los primeros 4 bytes)."""

    def __init__(self, reader: asyncio.StreamReader, prefix: bytes):
        self._reader = reader
        self._prefix = prefix

    async def readexactly(self, n: int) -> bytes:
        if self._prefix:
            if len(self._prefix) >= n:
                result = self._prefix[:n]
                self._prefix = self._prefix[n:]
                return result
            needed = n - len(self._prefix)
            rest = await self._reader.readexactly(needed)
            result = self._prefix + rest
            self._prefix = b""
            return result
        return await self._reader.readexactly(n)


class Connection:
    """Abstrae lectura/escritura de paquetes (header 128B + payload) sobre
    TCP crudo o WebSocket."""

    def __init__(self, reader, writer, is_websocket: bool):
        self.reader = reader
        self.writer = writer
        self.is_websocket = is_websocket

    async def read_packet(self) -> tuple[bytes, bytes] | None:
        if self.is_websocket:
            data = await ws_transport.read_frame(self.reader)
            if data is None or len(data) < proto.HEADER_LEN:
                return None
            return data[: proto.HEADER_LEN], data[proto.HEADER_LEN :]
        try:
            header = await self.reader.readexactly(proto.HEADER_LEN)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        length = proto.payload_length(header)
        payload = b""
        if length > 0:
            try:
                payload = await self.reader.readexactly(length)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
        return header, payload

    async def write_packet(self, header: bytes, payload: bytes = b"") -> None:
        data = header + payload
        if self.is_websocket:
            self.writer.write(ws_transport.build_frame(data))
        else:
            self.writer.write(data)
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()


async def detect_transport(reader, writer) -> Connection:
    peek = await reader.readexactly(4)
    if peek == b"GET ":
        await ws_transport.do_handshake(reader, writer)
        return Connection(reader, writer, True)
    return Connection(PeekedReader(reader, peek), writer, False)


class CaptureUnavailable(Exception):
    """La captura no se pudo crear. El fallo se recuerda: sin esto cada cliente
    que reconecta reintentaria el portal y volcaria un traceback, y la tablet
    entra en un loop de reconexion que llena el log de basura."""


class SharedCapture:
    """Una sola instancia de captura compartida entre clientes (es 'la' pantalla
    extendida del PC). Se crea de forma diferida, la primera vez que alguien
    conecta."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._capture: VirtualMonitorCapture | None = None
        self._lock = asyncio.Lock()
        self._failure: str | None = None

    async def get_or_create(self) -> VirtualMonitorCapture:
        async with self._lock:
            if self._failure is not None:
                raise CaptureUnavailable(self._failure)
            if self._capture is None:
                settings = self._settings
                cap = VirtualMonitorCapture(
                    settings.width, settings.height,
                    jpeg_quality=settings.jpeg_quality,
                    source_type=settings.source_type,
                    cursor_mode=settings.cursor_mode,
                    persist_permissions=settings.persist_permissions,
                    adaptive_quality=settings.adaptive_quality,
                    jpeg_quality_min=settings.jpeg_quality_min,
                    jpeg_quality_max=settings.jpeg_quality_max,
                    target_latency=settings.target_latency_ms / 1000,
                )
                loop = asyncio.get_event_loop()
                try:
                    await loop.run_in_executor(None, cap.start)
                except Exception as exc:
                    cap.stop()
                    self._failure = str(exc)
                    log.error("No se pudo crear la captura: %s", exc)
                    log.error(
                        "Revisa que estes en una sesion Wayland con un compositor que "
                        "soporte el portal ScreenCast (Plasma 6+, GNOME 42+) y que "
                        "xdg-desktop-portal este corriendo."
                    )
                    raise CaptureUnavailable(self._failure) from exc
                self._capture = cap
            return self._capture


async def handle_client(reader, writer, shared_capture: SharedCapture) -> None:
    addr = writer.get_extra_info("peername")
    log.info("Nueva conexion desde %s", addr)

    # Sin esto, el algoritmo de Nagle puede retener paquetes chicos (el
    # FlowControlAck que controla cuando mandamos el siguiente frame, los
    # headers de Touch/Mouse) hasta que se junten con mas datos o venza el
    # timer (~40ms) -- en un protocolo de pedido/respuesta como este eso se
    # siente como lentitud constante, no picos puntuales.
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    try:
        conn = await detect_transport(reader, writer)
    except (asyncio.IncompleteReadError, ConnectionError):
        return

    try:
        await handle_connection(conn, addr, shared_capture)
    except CaptureUnavailable as exc:
        log.warning("Conexion de %s rechazada: %s", addr, exc)
        conn.close()
    except (ConnectionError, OSError):
        conn.close()


async def handle_connection(conn, addr, shared_capture: SharedCapture) -> None:
    """Maneja una sesion completa (handshake + sender/receiver) sobre una
    Connection ya establecida."""
    result = await conn.read_packet()
    if result is None:
        conn.close()
        return
    header, _ = result
    if proto.header_type(header) != proto.HeaderType.IDENTIFICATION:
        log.warning("Primer paquete de %s no fue Identification (tipo=%s), cerrando",
                    addr, proto.header_type(header))
        conn.close()
        return
    ident = proto.IdentificationPacket.parse(header)
    log.info("Cliente identificado (%s): %r", addr, ident)
    log.debug("Identification completa de %s: %s", addr, ident.fields())
    log.debug("Identification header de %s: %s", addr, header.hex())

    # El tamano del framebuffer y la calidad JPEG salen de config.ini. La app
    # NO hace "fit to screen": muestra el frame a 1:1, asi que un framebuffer
    # mas chico que la pantalla aparece como un rectangulo en una esquina. La
    # SurfaceView real de la app es SIEMPRE 1920x1200 (confirmado con logcat:
    # "addSurfaceChangedCallback ... 0,0-1920,1200"), sin importar lo que el
    # cliente reporte en su Identification.
    capture = await shared_capture.get_or_create()

    # Sin esto la app se queda mostrando "Display off" indefinidamente aunque
    # ya le estemos mandando FrameBuffer -- ver protocol.py build_visibility_header.
    await conn.write_packet(bytes(proto.build_visibility_header(True)))

    vinput = VirtualInput(
        capture.conn, capture.session_handle, capture.stream_node_id,
        capture.stream_width, capture.stream_height,
        ident.effective_width(), ident.effective_height(),
    )
    ack_event = asyncio.Event()
    ack_event.set()  # listo para mandar el primer frame sin esperar ACK previo
    stop_event = asyncio.Event()
    seen_types: collections.Counter = collections.Counter()
    sent_at: float | None = None
    frames_sent = 0
    bytes_sent = 0
    window_start = time.monotonic()

    async def sender() -> None:
        nonlocal sent_at, frames_sent, bytes_sent, window_start
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(ack_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            ack_event.clear()
            # timeout corto para el keep-alive: si la pantalla esta estatica y
            # repetimos el ultimo frame, queremos hacerlo a un ritmo razonable
            # (~5fps) para que el cliente no interprete la espera como ancho de
            # banda bajo.
            jpeg = await loop.run_in_executor(None, capture.get_frame, 0.2)
            if jpeg is None:
                ack_event.set()
                continue
            fb_header = proto.build_framebuffer_header(
                payload_len=len(jpeg),
                width=capture.width,
                height=capture.height,
                pitch=capture.width * 3,
                color_format=proto.ColorType.RGB24,
                pos_x=0,
                pos_y=0,
                pos_x2=capture.width,
                pos_y2=capture.height,
                compression_type=proto.CompressionType.MJPEG,
                quality=capture.jpeg_quality,
                subsampling=proto.TJSamp.SAMP_420,
                fragment_info=0,
            )
            try:
                sent_at = time.monotonic()
                await conn.write_packet(fb_header, jpeg)
                frames_sent += 1
                bytes_sent += len(jpeg)
            except (ConnectionError, OSError):
                stop_event.set()
                break
            now = time.monotonic()
            if now - window_start >= 5.0:
                latency = capture.latency
                log.info(
                    "Stats: %.1f fps, %.0f KB/frame, latencia ACK %s, jpeg=%d, %dx%d",
                    frames_sent / (now - window_start),
                    bytes_sent / frames_sent / 1024,
                    f"{latency * 1000:.0f} ms" if latency else "n/d",
                    capture.jpeg_quality, capture.width, capture.height,
                )
                frames_sent = 0
                bytes_sent = 0
                window_start = now

    async def receiver() -> None:
        nonlocal sent_at
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            result = await conn.read_packet()
            if result is None:
                stop_event.set()
                break
            header, _payload = result
            htype = proto.header_type(header)
            seen_types[htype] += 1
            log.debug("<- %s payload=%dB header=%s",
                      proto.header_name(htype), proto.payload_length(header), header.hex())
            if htype == proto.HeaderType.FLOW_CONTROL_ACK:
                ack_event.set()
                if sent_at is not None:
                    capture.note_ack_latency(time.monotonic() - sent_at)
                    sent_at = None
            elif htype == proto.HeaderType.TOUCH:
                # vinput.* hace una llamada D-Bus sincrona (call_sync) -- sin
                # el executor, cada touch/mouse/key bloquearia el loop entero
                # de asyncio (frenando tambien el envio de frames), que es
                # justo la lentitud reportada al probar con la tablet real.
                await loop.run_in_executor(None, vinput.handle_touch, proto.TouchPacket.parse(header))
            elif htype == proto.HeaderType.PEN:
                # La tablet manda los toques del touchscreen por aqui (ver
                # protocol.PenPacket). Sin esto el input se descarta entero.
                await loop.run_in_executor(None, vinput.handle_pen, proto.PenPacket.parse(header))
            elif htype == proto.HeaderType.MOUSE:
                await loop.run_in_executor(None, vinput.handle_mouse, proto.MousePacket.parse(header))
            elif htype == proto.HeaderType.KEYBOARD:
                await loop.run_in_executor(None, vinput.handle_keyboard, proto.KeyboardPacket.parse(header))
            elif htype == proto.HeaderType.DISCONNECT:
                log.info("Cliente %s mando Disconnect", addr)
                stop_event.set()
                break
            elif htype == proto.HeaderType.PING:
                pass  # TODO: responder Pong si se confirma que la app lo requiere
            else:
                # A WARNING y no DEBUG: un tipo que no manejamos puede ser
                # drift del protocolo y tiene que verse sin --debug.
                log.warning("Paquete %s sin manejar de %s: header=%s",
                            proto.header_name(htype), addr, header.hex())

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    await stop_event.wait()
    sender_task.cancel()
    receiver_task.cancel()
    vinput.close()
    conn.close()
    log.info("Paquetes recibidos de %s: %s", addr, ", ".join(
        f"{proto.header_name(t)}={c}" for t, c in sorted(seen_types.items())))
    log.info("Conexion cerrada: %s", addr)


async def run_server(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("Configuracion: %s", settings.describe())

    shared_capture = SharedCapture(settings)
    if settings.discovery_enabled:
        await start_discovery_responder(settings.port, settings.name)

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, shared_capture), "0.0.0.0", settings.port
    )
    log.info("Servidor spacedesk-linux escuchando en puerto %d (monitor virtual se crea al conectar)",
              settings.port)

    async with server:
        await server.serve_forever()


def main(settings: Settings) -> None:
    try:
        asyncio.run(run_server(settings))
    except KeyboardInterrupt:
        pass
