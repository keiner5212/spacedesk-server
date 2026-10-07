"""
Transporte USB (Android Open Accessory) para el protocolo spacedesk.

Expone la misma interfaz que server.Connection (read_packet/write_packet/close)
pero sobre los endpoints bulk de un accesorio AOA en vez de un socket TCP/WS,
para poder reusar handle_connection() sin cambios.

Resumen del handshake AOA (ver memoria del proyecto para el detalle completo
de la sesion de depuracion): la PC actua como host USB, manda los strings de
identificacion via control transfers, y la tablet se re-enumera como
accesorio Google (18d1:2dxx) con un par de endpoints bulk vendor-specific.

HALLAZGO CRITICO: la app filtra el accesorio por
`UsbAccessory.getDescription().endsWith(" (spacedesk)")`
(SAActivityDisplayUsb.i1() en el APK decompilado) -- si el string de
description (indice AOA 2) no termina asi, la app nunca abre el pipe y no
se puede distinguir del lado del host (la tablet entra en accessory mode
igual, pero no llega nada por el endpoint IN).

pyusb es una dependencia opcional: si no esta instalado, este modulo se
importa sin error (USB_AVAILABLE queda en False) para no romper el
transporte TCP/WebSocket existente en instalaciones que no necesitan USB.
"""

import asyncio
import logging
import time

from . import protocol as proto

log = logging.getLogger("spacedesk.usb")

try:
    import usb.core
    import usb.util

    USB_AVAILABLE = True
except ImportError:
    USB_AVAILABLE = False

# Tablet HONOR NDL-W09 en modo normal -- ajustar si se usa otro dispositivo.
NORMAL_VID, NORMAL_PID = 0x339B, 0x107D

AOA_VID = 0x18D1
AOA_PIDS = (0x2D00, 0x2D01, 0x2D02, 0x2D03, 0x2D04, 0x2D05)
EP_IN, EP_OUT = 0x81, 0x01
USB_INTERFACE = 0

ACCESSORY_GET_PROTOCOL = 51
ACCESSORY_SEND_STRING = 52
ACCESSORY_START = 53

# indice 2 (description) DEBE terminar en " (spacedesk)" -- ver docstring del modulo.
AOA_STRINGS = {
    0: "datronicsoft",
    1: "spacedesk",
    2: "Linux PC (spacedesk)",
    3: "1.0",
    4: "https://www.spacedesk.net",
    5: "0000000012345678",
}

READ_CHUNK = 65536
READ_TIMEOUT_MS = 2000
WRITE_TIMEOUT_MS = 5000
REENUM_TIMEOUT_S = 10.0
RETRY_DELAY_S = 3.0


def _find_accessory():
    for pid in AOA_PIDS:
        dev = usb.core.find(idVendor=AOA_VID, idProduct=pid)
        if dev is not None:
            return dev
    return None


def _do_handshake(dev) -> None:
    ret = dev.ctrl_transfer(0xC0, ACCESSORY_GET_PROTOCOL, 0, 0, 2, timeout=2000)
    proto_version = ret[0] | (ret[1] << 8)
    if proto_version == 0:
        raise RuntimeError("El dispositivo USB conectado no soporta AOA")
    for idx, val in AOA_STRINGS.items():
        dev.ctrl_transfer(0x40, ACCESSORY_SEND_STRING, 0, idx, val.encode() + b"\x00", timeout=2000)
    dev.ctrl_transfer(0x40, ACCESSORY_START, 0, 0, None, timeout=2000)
    usb.util.dispose_resources(dev)


def wait_for_accessory():
    """Bloqueante. Devuelve un device ya en accessory mode con la interfaz
    reclamada, o None si no hay ninguna tablet conectada (ni en modo normal
    ni en accessory) en este momento -- el llamador debe reintentar."""
    dev = _find_accessory()
    if dev is None:
        normal = usb.core.find(idVendor=NORMAL_VID, idProduct=NORMAL_PID)
        if normal is None:
            return None
        log.info("Tablet detectada en modo normal (%04x:%04x), iniciando handshake AOA",
                  NORMAL_VID, NORMAL_PID)
        try:
            _do_handshake(normal)
        except usb.core.USBError as e:
            log.warning("Fallo el handshake AOA: %s", e)
            return None
        deadline = time.monotonic() + REENUM_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(0.5)
            dev = _find_accessory()
            if dev is not None:
                break
        if dev is None:
            log.warning("La tablet no re-enumero en accessory mode tras %.0fs", REENUM_TIMEOUT_S)
            return None

    try:
        if dev.is_kernel_driver_active(USB_INTERFACE):
            dev.detach_kernel_driver(USB_INTERFACE)
    except (usb.core.USBError, NotImplementedError):
        pass
    # set_configuration() es a nivel de DISPOSITIVO completo (no de interfaz) --
    # si otro proceso (p.ej. el daemon adb, en modo accessory+adb) ya configuro
    # el dispositivo, volver a llamarla tira "Resource busy" sin necesidad,
    # ya que afectaria tambien a la interfaz que ese otro proceso esta usando.
    try:
        dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
    usb.util.claim_interface(dev, USB_INTERFACE)
    log.info("Accesorio USB listo, interfaz %d reclamada", USB_INTERFACE)
    return dev


class UsbConnection:
    """Misma interfaz que server.Connection (read_packet/write_packet/close).

    Las llamadas pyusb son bloqueantes (libusb sincrono), asi que todo se
    despacha a un executor para no congelar el loop de asyncio -- mismo
    patron que ya se usa en server.py para las llamadas D-Bus de input."""

    is_websocket = False

    def __init__(self, dev):
        self._dev = dev
        self._buf = bytearray()
        self._closed = False

    def _read_chunk(self) -> bool:
        """Hace un bulk read y lo agrega al buffer interno. Devuelve False si
        el dispositivo se desconecto (la tablet o el cable)."""
        while not self._closed:
            try:
                data = self._dev.read(EP_IN, READ_CHUNK, timeout=READ_TIMEOUT_MS)
            except usb.core.USBError as e:
                if e.errno == 110:  # ETIMEDOUT: nada en esta ventana, normal, reintentar
                    continue
                return False
            self._buf += bytes(data)
            return True
        return False

    def _blocking_read_packet(self):
        while len(self._buf) < proto.HEADER_LEN:
            if not self._read_chunk():
                return None
        header = bytes(self._buf[: proto.HEADER_LEN])
        length = proto.payload_length(header)
        while len(self._buf) < proto.HEADER_LEN + length:
            if not self._read_chunk():
                return None
        payload = bytes(self._buf[proto.HEADER_LEN : proto.HEADER_LEN + length])
        del self._buf[: proto.HEADER_LEN + length]
        return header, payload

    def _blocking_write(self, data: bytes) -> None:
        # dev.write() hace UNA sola transferencia bulk y puede devolver menos
        # bytes de los pedidos sin avisar (no es un error) -- hay que loopear
        # hasta mandar todo, igual que SAUsbPipeStream.a() (read) del lado
        # Android loopea para recibir todo lo que nosotros escribimos aca.
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            n = self._dev.write(EP_OUT, view[sent:], timeout=WRITE_TIMEOUT_MS)
            if n <= 0:
                raise usb.core.USBError("Escritura USB devolvio 0 bytes")
            sent += n

    async def read_packet(self):
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._blocking_read_packet)
        except usb.core.USBError as e:
            log.info("Conexion USB perdida durante lectura: %s", e)
            return None

    async def write_packet(self, header: bytes, payload: bytes = b"") -> None:
        # Header y payload van en transferencias bulk SEPARADAS, nunca
        # concatenados en una sola escritura. La API UsbAccessory de Android
        # entrega los datos al FileInputStream del lado app por "transferencia
        # USB completa" -- si la app pide leer 128 bytes (el header) pero nuestra
        # UNICA escritura junto header+payload llega como una sola transferencia
        # mas grande, esa lectura consume TODA la transferencia y el resto del
        # payload se pierde (no queda bufferizado para la siguiente llamada a
        # read()), dejando al receptor esperando para siempre datos que ya se
        # descartaron. Confirmado con logcat real: el log se quedaba trabado en
        # "SATaskLoopedFrameBufferProcessorUsb OnExecute - getting packet" sin
        # error ni progreso.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._blocking_write, header)
        if payload:
            await loop.run_in_executor(None, self._blocking_write, payload)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            usb.util.release_interface(self._dev, USB_INTERFACE)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self._dev)


async def usb_acceptor_loop(on_connection):
    """Loop perpetuo: espera la tablet por USB, atiende una sesion completa,
    y al desconectarse (cable, o cierre de la app) vuelve a esperar.

    on_connection(conn, addr) es la corutina que maneja una sesion completa
    (la misma que usa el transporte TCP/WebSocket, via handle_connection)."""
    if not USB_AVAILABLE:
        log.warning("pyusb no esta instalado -- transporte USB deshabilitado. "
                     "Instalar con pip (en un venv --system-site-packages para "
                     "conservar PyGObject) para habilitarlo.")
        return

    loop = asyncio.get_event_loop()
    log.info("Esperando tablet por USB (Android Open Accessory)...")
    while True:
        try:
            dev = await loop.run_in_executor(None, wait_for_accessory)
        except usb.core.USBError as e:
            # Puede pasar con un device "stale" (de una sesion anterior que ya
            # se cerro del lado de la tablet) -- nunca debe tirar abajo todo
            # el servidor (WiFi incluido), solo reintentar.
            log.warning("Error USB esperando accesorio: %s", e)
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        if dev is None:
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        conn = UsbConnection(dev)
        log.info("Sesion USB iniciada")
        start = time.monotonic()
        try:
            await on_connection(conn, "USB")
        except Exception:
            log.exception("Error en la sesion USB")
        finally:
            conn.close()
        # Si la sesion termino casi instantaneamente, es casi seguro que el
        # dispositivo ya estaba en accessory mode de una sesion anterior (p.ej.
        # el servidor se reinicio en caliente) con datos viejos pendientes en
        # el buffer USB (el primer paquete leido no fue Identification sino
        # basura de esa sesion vieja) -- resetear antes de reintentar, sino el
        # siguiente intento vuelve a leer la misma basura y entra en un loop
        # rapido sin nunca lograr una sesion real.
        if time.monotonic() - start < 1.0:
            try:
                dev.reset()
                log.info("Sesion USB muy corta, dispositivo reseteado para el proximo intento")
            except usb.core.USBError:
                pass
            await asyncio.sleep(1.0)
        log.info("Sesion USB finalizada, esperando reconexion")
