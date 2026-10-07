"""
Protocolo binario de spacedesk (puerto TCP/WS 28252).

Todos los offsets y enums fueron confirmados por auditoría cruzada de dos fuentes
oficiales de datronicsoft (no por adivinanza):
  1. El cliente HTML5 oficial (spacedesk.min.js, TypeScript compilado sin ofuscar
     nombres de namespace/clase).
  2. El APK Android nativo v2.1.33 decompilado con jadx (clase AbstractC0674b para
     los enums, C0686e para los offsets del header, ofuscadas con ProGuard pero con
     los nombres de enum intactos).

Las partes marcadas "NO CONFIRMADO" son aproximaciones razonables, no verificadas
línea por línea contra el código fuente decompilado -- requieren validación empírica
(adb logcat con la app real) antes de confiar en ellas para producción.

Ver memoria del proyecto (sesión de ingeniería inversa) para la cita exacta de cada
clase/método fuente de cada hallazgo.
"""

import enum
import struct

HEADER_LEN = 128
PROTOCOL_VERSION_MAJOR = 4
PROTOCOL_VERSION_MINOR = 8

DISCOVERY_PORT = 28252
DISCOVERY_MAGIC = b"SPACEDESK-NET-CLIENT\x00"


class HeaderType(enum.IntEnum):
    IDENTIFICATION = 0
    PING = 1
    FRAMEBUFFER = 2
    VISIBILITY = 3
    DISPLAY_SETTINGS = 4
    UNUSED_01 = 5
    EVT_DISPLAY_SETTINGS = 6
    FLOW_CONTROL_ACK = 7
    DISCONNECT = 8
    ROTATION = 9
    MOUSE = 10
    KEYBOARD = 11
    TOUCH = 12
    PEN = 13
    AUDIO = 14
    MAX = 15


class ClientType(enum.IntEnum):
    DISPLAY_MONITOR = 0
    DISPLAY_MONITOR_WEB_BROWSER = 1
    RESERVED_01 = 2
    AUDIO = 3
    MAX = 4


class CompressionType(enum.IntEnum):
    OFF = 0
    YUV_PLAIN = 1
    UNUSED_01 = 2
    MJPEG = 3
    H264 = 4
    CHOOSE_BEST = 5
    MAX = 6


class OsType(enum.IntEnum):
    UNKNOWN = 0
    WINDOWS_NATIVE = 1
    WINDOWS_UWP = 2
    ANDROID = 3
    IOS = 4
    MAX = 5


class LicenseType(enum.IntEnum):
    NONE = 0
    NONCOMMERCIAL_BASIC = 1
    NONCOMMERCIAL_ADVANCED = 2
    BUSINESS_ANDROID = 3
    BUSINESS_WINDOWS_UWP = 4
    BUSINESS_IOS = 5
    MAX = 6


class ColorType(enum.IntEnum):
    RGB8 = 0
    RGB16 = 1
    RGB24 = 2
    RGBX32 = 3
    RGBA32 = 4
    YUV444 = 5
    YUV422 = 6
    YUV420 = 7
    MAX = 8


class TJSamp(enum.IntEnum):
    SAMP_444 = 0
    SAMP_422 = 1
    SAMP_420 = 2
    SAMP_GRAY = 3
    SAMP_440 = 4
    SAMP_411 = 5


class TouchAction(enum.IntEnum):
    """Códigos del modo 'moderno' (multitouch) usado por la app nativa, W1.e().
    Confirmado: C0677b2.d() = MotionEvent.getAction() (DOWN=0,UP=1,MOVE=2,CANCEL=3
    estándar de Android), remapeado por W1.e() a estos códigos de protocolo."""
    MOVE = 1
    DOWN = 3
    UP = 5  # también usado para CANCEL


# ---------------------------------------------------------------------------
# Helpers de empaquetado little-endian (confirmado en O2.java: ByteOrder.LITTLE_ENDIAN
# en todas las lecturas/escrituras, y en el JS via getInteger32Value/assignInteger32).
# ---------------------------------------------------------------------------

def empty_header() -> bytearray:
    return bytearray(HEADER_LEN)


def set_i32(buf: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<i", buf, offset, value)


def get_i32(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<i", buf, offset)[0]


def set_u32(buf: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", buf, offset, value & 0xFFFFFFFF)


def get_u32(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buf, offset)[0]


def set_i16(buf: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<h", buf, offset, value)


def get_i16(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<h", buf, offset)[0]


def header_type(header: bytes) -> int:
    """Lee offset 0: tipo de mensaje (HeaderType), igual en todos los tipos de paquete."""
    return get_i32(header, 0)


def payload_length(header: bytes) -> int:
    """Lee offset 4: cantidad de bytes de payload que siguen al header de 128 bytes."""
    return get_i32(header, 4)


# ---------------------------------------------------------------------------
# Identification (cliente -> servidor, primer paquete tras conectar).
# Offsets confirmados contra C0686e + IdentificationHeader.GetBytes (JS).
# ---------------------------------------------------------------------------

class IdentificationPacket:
    __slots__ = (
        "version_major", "version_minor", "client_type", "os_type",
        "compression", "subsampling", "quality", "frame_rate",
        "resolution_mode", "width", "height", "width_custom", "height_custom",
        "license_type",
    )

    @classmethod
    def parse(cls, header: bytes) -> "IdentificationPacket":
        pkt = cls()
        pkt.version_major = get_i32(header, 8)
        pkt.version_minor = get_i32(header, 12)
        pkt.client_type = get_i32(header, 16)
        pkt.os_type = get_i32(header, 20)
        pkt.compression = get_i32(header, 24)
        pkt.subsampling = get_i32(header, 28)
        pkt.quality = get_i32(header, 32)
        pkt.frame_rate = get_i16(header, 44)
        pkt.resolution_mode = get_i32(header, 48)
        pkt.width = get_i32(header, 52)
        pkt.width_custom = get_i32(header, 56)
        pkt.height = get_i32(header, 88)
        pkt.height_custom = get_i32(header, 92)
        pkt.license_type = get_i32(header, 124)
        return pkt

    def effective_width(self) -> int:
        return self.width_custom if self.resolution_mode == 2 and self.width_custom else self.width

    def effective_height(self) -> int:
        return self.height_custom if self.resolution_mode == 2 and self.height_custom else self.height

    def __repr__(self) -> str:
        return (
            f"IdentificationPacket(version={self.version_major}.{self.version_minor}, "
            f"client_type={self.client_type}, os_type={self.os_type}, "
            f"compression={self.compression}, quality={self.quality}, "
            f"resolution={self.effective_width()}x{self.effective_height()})"
        )


# ---------------------------------------------------------------------------
# FrameBuffer (servidor -> cliente). Offsets confirmados por los getters de
# C0686e usados en x2.j() (validación del lado cliente) y FrameBufferHeader (JS).
# ---------------------------------------------------------------------------

def build_visibility_header(is_visible: bool) -> bytearray:
    """Servidor -> cliente. offset8=isVisible(0/1). Confirmado por el validador
    del cliente (x2.j(): exige r() in [0,1] para HeaderType.VISIBILITY) y por
    q2.m(), que solo dispara el evento 'display ON/OFF' de la UI al recibir
    este paquete -- sin él la app se queda mostrando 'Display off' aunque ya
    estén llegando paquetes FrameBuffer por debajo."""
    h = empty_header()
    set_i32(h, 0, HeaderType.VISIBILITY)
    set_i32(h, 4, 0)
    set_i32(h, 8, 1 if is_visible else 0)
    return h


def build_framebuffer_header(
    payload_len: int,
    width: int,
    height: int,
    pitch: int,
    color_format: int,
    pos_x: int,
    pos_y: int,
    pos_x2: int,
    pos_y2: int,
    compression_type: int,
    quality: int,
    subsampling: int,
    fragment_info: int = 0,
) -> bytearray:
    """fragment_info=0 siempre: ver t2.g() -- es un flag de pipeline de decodificación
    del cliente, no fragmentación de red. 0 = 'frame completo, presentar ya', que es
    el comportamiento correcto y simple para el servidor."""
    h = empty_header()
    set_i32(h, 0, HeaderType.FRAMEBUFFER)
    set_i32(h, 4, payload_len)
    set_i32(h, 8, width)
    set_i32(h, 12, height)
    set_i32(h, 16, pitch)
    set_i32(h, 20, color_format)
    set_i32(h, 24, pos_x)
    set_i32(h, 28, pos_y)
    set_i32(h, 32, pos_x2)
    set_i32(h, 36, pos_y2)
    set_i32(h, 40, compression_type)
    set_i32(h, 44, subsampling)
    set_i32(h, 48, quality)
    set_i32(h, 64, fragment_info)
    return h


# ---------------------------------------------------------------------------
# Touch (cliente -> servidor). CONFIRMADO con alta confianza: W1.d() + C0677b2.
# ---------------------------------------------------------------------------

class TouchPacket:
    __slots__ = ("x", "y", "res_x", "res_y", "action", "pointer_id", "timestamp_ms")

    @classmethod
    def parse(cls, header: bytes) -> "TouchPacket":
        pkt = cls()
        pkt.x = get_i32(header, 8)
        pkt.y = get_i32(header, 12)
        pkt.res_x = get_i32(header, 16)
        pkt.res_y = get_i32(header, 20)
        pkt.action = get_i16(header, 24)
        pkt.pointer_id = get_i16(header, 26)
        pkt.timestamp_ms = get_i32(header, 28)
        return pkt

    def __repr__(self) -> str:
        return (
            f"TouchPacket(x={self.x}, y={self.y}, res={self.res_x}x{self.res_y}, "
            f"action={self.action}, pointer_id={self.pointer_id})"
        )


# ---------------------------------------------------------------------------
# Mouse (cliente -> servidor). Layout de offsets CONFIRMADO (V1.c() + JS), pero
# los valores exactos del bitmask de botones (Wheel/LeftDown/LeftUp/RightDown/
# RightUp) NO están confirmados con evidencia decompilada -- solo se vio el
# nombre del enum en el JS, no su valor numérico. Aproximación razonable abajo,
# pendiente de validar con adb logcat.
# ---------------------------------------------------------------------------

class MousePacket:
    __slots__ = ("x", "y", "wheel_delta", "button_flags", "extra_flags")

    @classmethod
    def parse(cls, header: bytes) -> "MousePacket":
        pkt = cls()
        pkt.x = get_i32(header, 8)
        pkt.y = get_i32(header, 12)
        pkt.wheel_delta = get_i32(header, 16)
        pkt.button_flags = get_i32(header, 20)
        pkt.extra_flags = get_i32(header, 24)
        return pkt

    def __repr__(self) -> str:
        return (
            f"MousePacket(x={self.x}, y={self.y}, wheel={self.wheel_delta}, "
            f"buttons=0x{self.button_flags:x})"
        )


# ---------------------------------------------------------------------------
# Keyboard (cliente -> servidor). Layout de offsets visto en S1.java, pero la
# distinción key-up/key-down NO se confirmó en el análisis estático -- gap
# abierto, requiere validación empírica.
# ---------------------------------------------------------------------------

class KeyboardPacket:
    __slots__ = ("vkeycode", "scancode", "flags")

    @classmethod
    def parse(cls, header: bytes) -> "KeyboardPacket":
        pkt = cls()
        pkt.vkeycode = get_i32(header, 8)
        pkt.scancode = get_i32(header, 12)
        pkt.flags = get_i32(header, 16)
        return pkt
