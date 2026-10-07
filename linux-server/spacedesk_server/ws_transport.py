"""
Framing WebSocket mínimo (RFC 6455) para el transporte usado por el visor HTML5
de spacedesk. La app Android nativa NO usa esto -- usa TCP crudo directamente
(ver protocol.py / memoria del proyecto). Implementado a mano para poder mezclar
ambos transportes en el mismo puerto 28252 sin depender de una librería que
asuma que ella controla todo el accept() de la conexión.
"""

import asyncio
import base64
import hashlib
import struct

WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


async def read_http_handshake(reader: asyncio.StreamReader) -> dict[str, str]:
    """Asume que ya se consumieron los primeros 4 bytes ('GET '); lee el resto
    de la request line + headers hasta la línea vacía."""
    headers: dict[str, str] = {}
    rest_of_line = await reader.readuntil(b"\r\n")
    while True:
        line = await reader.readuntil(b"\r\n")
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


def compute_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_MAGIC.decode()).encode("latin-1")).digest()
    return base64.b64encode(digest).decode("ascii")


async def do_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    headers = await read_http_handshake(reader)
    client_key = headers.get("sec-websocket-key", "")
    accept = compute_accept_key(client_key)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(response.encode("latin-1"))
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """Lee un frame WebSocket binario completo y devuelve su payload desenmascarado.
    Devuelve None si el cliente cerró la conexión (frame de Close u opcode 0x8)."""
    first2 = await reader.readexactly(2)
    b0, b1 = first2[0], first2[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F

    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]

    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""

    if masked and payload:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    if opcode == 0x8:  # Close
        return None
    if opcode == 0x9:  # Ping -- el llamador no responde Pong explícitamente (best effort, v1)
        return b""
    return payload


def build_frame(payload: bytes) -> bytes:
    """Construye un frame WebSocket binario (opcode 0x2), sin máscara (servidor->cliente
    no requiere máscara según RFC 6455)."""
    length = len(payload)
    header = bytearray()
    header.append(0x80 | 0x2)  # FIN=1, opcode=binary
    if length <= 125:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + payload
