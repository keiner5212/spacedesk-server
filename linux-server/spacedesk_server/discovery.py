"""
Discovery: responde al broadcast UDP que manda la app Android al buscar
servidores en la LAN.

La peticion es `b"SPACEDESK-NET-CLIENT\\x00"` al puerto 28252 (5 veces por
interfaz). La RESPUESTA no es un eco de ese texto: la app recibe en un buffer
de 308 bytes y lo parsea como un struct binario (`ph.spacedesk...C2674f`), con
el nombre del servidor en UTF-16LE, la IP, el puerto, el tipo de OS y unos flags
de capabilities. Responder con el magic, como hacia la primera version de este
servidor, hacia que la app nunca viera el servidor. Ver
`protocol.build_discovery_response` para el layout.
"""

import asyncio
import logging

from .protocol import build_discovery_response, ipv4_to_int, set_u32

log = logging.getLogger("spacedesk.discovery")


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, server_name: str, port: int):
        self.server_name = server_name
        self.port = port
        self.transport = None
        # La respuesta es siempre la misma (nombre, puerto y flags no cambian),
        # solo patcheamos la IP de origen, que ademas la app sobreescribe al
        # parsear. Asi no re-armamos 308 bytes por cada request.
        self._response = bytearray(build_discovery_response(server_name, port, "0.0.0.0"))

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if not data.startswith(b"SPACEDESK-NET-CLIENT"):
            return
        # Se responde a CADA request, sin dedup. La app manda el mismo request 5
        # veces por rafaga a proposito: su socket solo vive ~300 ms por ronda
        # y el wiFi pierde paquetes, asi que con una sola respuesta por rafaga
        # hay rondas enteras sin respuesta. Contestar a todas maximize la
        # chance de que al menos una llegue antes de que cierre el socket.
        try:
            set_u32(self._response, 256, ipv4_to_int(addr[0]))
        except ValueError as exc:
            log.warning("Discovery: origen invalido %s: %s", addr[0], exc)
            return
        self.transport.sendto(bytes(self._response), addr)
        log.debug("Discovery: respondiendo a %s con '%s' en %s:%d",
                  addr, self.server_name, addr[0], self.port)


async def start_discovery_responder(port: int, server_name: str) -> asyncio.DatagramTransport:
    loop = asyncio.get_event_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(server_name, port),
        local_addr=("0.0.0.0", port),
        # Sin reuse_port: si quedo otra instancia viva, el kernel reparte el
        # trafico entre las dos en vez de avisar. Con reuse_port el bind nuevo
        # "funciona", el cliente se conecta a la vieja y el discovery parece
        # intermitente sin ningun error. Ver port.free_port.
        allow_broadcast=True,
    )
    log.info("Discovery UDP escuchando en puerto %d (nombre: %s)", port, server_name)
    return transport