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
import logging
import socket

from . import protocol as proto
from . import usb_transport
from . import ws_transport
from .capture import VirtualMonitorCapture
from .discovery import start_discovery_responder
from .input import VirtualInput

log = logging.getLogger("spacedesk.server")

LISTEN_PORT = proto.DISCOVERY_PORT  # 28252, mismo puerto para datos y discovery (UDP aparte)
# La app NO hace "fit to screen": muestra el framebuffer a su tamaño real
# (1:1), así que un framebuffer más chico que la pantalla aparece como un
# rectángulo pequeño en una esquina en vez de llenarla (confirmado
# empíricamente). Por eso hay que usar exactamente la resolución que la app
# pide en su Identification (ident.effective_width/height), no un valor
# recalculado del lado servidor -- ver handle_client.


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


class SharedCapture:
    """Una sola instancia de captura compartida entre clientes (es 'la' pantalla
    extendida del PC). Se crea de forma diferida con la resolución que reporte
    el primer cliente que conecte -- el tamaño fijo por defecto no tenía en
    cuenta la resolución real de cada dispositivo."""

    def __init__(self):
        self._capture: VirtualMonitorCapture | None = None
        self._lock = asyncio.Lock()

    async def get_or_create(self, width: int, height: int, jpeg_quality: int) -> VirtualMonitorCapture:
        async with self._lock:
            if self._capture is None:
                loop = asyncio.get_event_loop()
                cap = VirtualMonitorCapture(width, height, jpeg_quality=jpeg_quality)
                await loop.run_in_executor(None, cap.start)
                self._capture = cap
                log.info("Monitor virtual creado a demanda: %dx%d, calidad=%d",
                         width, height, jpeg_quality)
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

    await handle_connection(conn, addr, shared_capture)


async def handle_connection(conn, addr, shared_capture: SharedCapture) -> None:
    """Maneja una sesion completa (handshake + sender/receiver) sobre una
    Connection ya establecida -- usado tanto por TCP/WebSocket (handle_client)
    como por USB (usb_transport.usb_acceptor_loop), que solo difieren en como
    se construye el objeto `conn`."""
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

    # La SurfaceView donde la app dibuja es SIEMPRE 1920x1200 (confirmado con
    # logcat real: "addSurfaceChangedCallback ... 0,0-1920,1200"), sin importar
    # lo que el cliente reporte en su Identification -- por WiFi la app reporta
    # justo 1920x1200, pero por USB reporta 1920x1080 (inconsistencia propia de
    # la app entre transportes). Usar siempre el tamano real de la superficie
    # en vez de ident.effective_width/height().
    #
    # Calidad mas alta para USB: el cuello de botella de WiFi (ver memoria del
    # proyecto, ~200-400ms de espera por frame) no aplica sobre USB2 bulk
    # (~480Mbps), asi que no hace falta comprimir tan agresivo.
    jpeg_quality = 95 if addr == "USB" else 55
    capture = await shared_capture.get_or_create(1920, 1200, jpeg_quality)

    # Sin esto la app se queda mostrando "Display off" indefinidamente aunque
    # ya le estemos mandando FrameBuffer -- ver protocol.py build_visibility_header.
    await conn.write_packet(bytes(proto.build_visibility_header(True)))

    vinput = VirtualInput(
        capture.conn, capture.remote_desktop_session_path, capture.stream_path,
        capture.width, capture.height,
    )
    ack_event = asyncio.Event()
    ack_event.set()  # listo para mandar el primer frame sin esperar ACK previo
    stop_event = asyncio.Event()

    async def sender() -> None:
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
                await conn.write_packet(fb_header, jpeg)
            except (ConnectionError, OSError):
                stop_event.set()
                break

    async def receiver() -> None:
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            result = await conn.read_packet()
            if result is None:
                stop_event.set()
                break
            header, _payload = result
            htype = proto.header_type(header)
            if htype == proto.HeaderType.FLOW_CONTROL_ACK:
                ack_event.set()
            elif htype == proto.HeaderType.TOUCH:
                # vinput.* hace una llamada D-Bus sincrona (call_sync) -- sin
                # el executor, cada touch/mouse/key bloquearia el loop entero
                # de asyncio (frenando tambien el envio de frames), que es
                # justo la lentitud reportada al probar con la tablet real.
                await loop.run_in_executor(None, vinput.handle_touch, proto.TouchPacket.parse(header))
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
                log.debug("Paquete tipo %s sin manejar de %s", htype, addr)

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    await stop_event.wait()
    sender_task.cancel()
    receiver_task.cancel()
    vinput.close()
    conn.close()
    log.info("Conexion cerrada: %s", addr)


async def run_server() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    shared_capture = SharedCapture()
    await start_discovery_responder()

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, shared_capture), "0.0.0.0", LISTEN_PORT
    )
    log.info("Servidor spacedesk-linux escuchando en puerto %d (monitor virtual se crea al conectar)",
              LISTEN_PORT)

    # Transporte USB (Android Open Accessory) en paralelo al TCP/WebSocket --
    # ambos comparten la misma SharedCapture, asi que es "la" misma pantalla
    # extendida sin importar por donde se conecte la tablet. Se auto-deshabilita
    # con un solo log.warning si pyusb no esta instalado (ver usb_transport.py).
    usb_task = asyncio.create_task(
        usb_transport.usb_acceptor_loop(lambda conn, addr: handle_connection(conn, addr, shared_capture))
    )

    async with server:
        await asyncio.gather(server.serve_forever(), usb_task)


def main() -> None:
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass
