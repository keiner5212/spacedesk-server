"""
Responde a los broadcasts UDP de discovery que manda la app Android al buscar
servidores en la LAN: magic string b"SPACEDESK-NET-CLIENT\\0" al puerto 28252
(confirmado en w2.java del APK decompilado). Si esto no responde, el usuario
puede igual ingresar la IP del servidor manualmente en la app -- no es bloqueante,
solo evita tener que escribir la IP a mano.
"""

import asyncio
import logging

from .protocol import DISCOVERY_MAGIC, DISCOVERY_PORT

log = logging.getLogger("spacedesk.discovery")


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if data.startswith(b"SPACEDESK-NET-CLIENT"):
            log.debug("Discovery request de %s", addr)
            # Eco simple: suficiente para que la app sepa que hay un servidor en esa IP.
            self.transport.sendto(DISCOVERY_MAGIC, addr)


async def start_discovery_responder() -> asyncio.DatagramTransport:
    loop = asyncio.get_event_loop()
    transport, _ = await loop.create_datagram_endpoint(
        DiscoveryProtocol,
        local_addr=("0.0.0.0", DISCOVERY_PORT),
        reuse_port=True,
        allow_broadcast=True,
    )
    log.info("Discovery UDP escuchando en puerto %d", DISCOVERY_PORT)
    return transport
