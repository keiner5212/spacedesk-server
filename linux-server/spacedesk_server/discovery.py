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
import struct

from .protocol import DISCOVERY_MAGIC, build_discovery_response

log = logging.getLogger("spacedesk.discovery")


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, server_name: str, port: int):
        self.server_name = server_name
        self.port = port
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if not data.startswith(b"SPACEDESK-NET-CLIENT"):
            return
        try:
            response = build_discovery_response(self.server_name, self.port, addr[0])
        except (ValueError, struct.error) as exc:
            # Nunca dejar que una peticion mal formada tumbe el endpoint: el
            # discovery es best effort, perderlo rompe el descubrimiento.
            log.warning("Discovery: no se pudo responder a %s: %s", addr, exc)
            return
        self.transport.sendto(response, addr)
        log.info("Discovery: respondiendo a %s con '%s' en %s:%d",
                 addr, self.server_name, addr[0], self.port)


async def start_discovery_responder(port: int, server_name: str) -> asyncio.DatagramTransport:
    loop = asyncio.get_event_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(server_name, port),
        local_addr=("0.0.0.0", port),
        reuse_port=True,
        allow_broadcast=True,
    )
    log.info("Discovery UDP escuchando en puerto %d (nombre: %s)", port, server_name)
    return transport