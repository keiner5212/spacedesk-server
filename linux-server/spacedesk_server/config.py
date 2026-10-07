"""Configuracion del servidor leida de un INI, con defaults utilizables.

Un solo archivo (`config.ini`, junto a `main.py`) concentra lo que antes estaba
esparcido en constantes: se ajusta sin tocar codigo.
"""

import configparser
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"

DEFAULTS = {
    "server": {
        "port": "28252",
        "log_level": "INFO",
        "name": "spacedesk-linux",
        # Frames sin confirmar a la vez. 1 = estricto con el protocolo.
        "in_flight_frames": "1",
    },
    "capture": {
        "width": "1600",
        "height": "900",
        "jpeg_quality": "55",
        # Ajuste automatico de la calidad segun la latencia medida.
        "adaptive_quality": "true",
        "jpeg_quality_min": "30",
        "jpeg_quality_max": "90",
        "target_fps": "20",
        # Cada reenvio del mismo frame gasta un round trip completo, y ese
        # round trip es justo donde se encola el proximo frame con novedad.
        # 0 desactiva el keep-alive (la app puede quejarse de ancho de banda).
        "keepalive_ms": "500",
        # 1 = oculto, 2 = dibujado en los pixeles del frame, 4 = metadata.
        "cursor_mode": "2",
        # Tipo de fuente del portal: 4 = VIRTUAL (monitor virtual real de KWin).
        # 1 = MONITOR (espeja un monitor fisico), 7 = cualquiera de los dos.
        "source_type": "4",
        # Recordar la autorizacion del portal para no mostrar el dialogo en
        # cada arranque (spec: persist_mode=2 + restore_token).
        "persist_permissions": "true",
    },
    "discovery": {
        "enabled": "true",
    },
}

CURSOR_MODES = (1, 2, 4)
SOURCE_MONITOR = 1
SOURCE_VIRTUAL = 4


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    port: int
    log_level: str
    name: str
    in_flight_frames: int
    width: int
    height: int
    jpeg_quality: int
    adaptive_quality: bool
    jpeg_quality_min: int
    jpeg_quality_max: int
    target_fps: int
    keepalive_ms: int
    cursor_mode: int
    source_type: int
    persist_permissions: bool
    discovery_enabled: bool

    def describe(self) -> str:
        return (
            f"puerto={self.port} nombre={self.name!r} en_vuelo={self.in_flight_frames} "
            f"framebuffer={self.width}x{self.height} "
            f"jpeg={self.jpeg_quality}{'*' if self.adaptive_quality else ''} fuente={self.source_type} "
            f"cursor={self.cursor_mode} discovery={'on' if self.discovery_enabled else 'off'}"
        )


def load(path: str | Path | None = None) -> Settings:
    parser = configparser.ConfigParser()
    parser.read_dict(DEFAULTS)
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if config_path.exists():
        read = parser.read(config_path)
        if not read:
            raise ConfigError(f"No se pudo leer la configuracion: {config_path}")
    elif path:
        raise ConfigError(f"No existe el archivo de configuracion: {config_path}")

    def get_int(section: str, option: str) -> int:
        raw = parser.get(section, option, fallback=DEFAULTS[section][option])
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"[{section}] {option} debe ser un numero entero: {raw!r}") from None

    def get_bool(section: str, option: str) -> bool:
        raw = parser.get(section, option, fallback=DEFAULTS[section][option]).strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"[{section}] {option} debe ser true o false: {raw!r}")

    settings = Settings(
        port=get_int("server", "port"),
        log_level=parser.get("server", "log_level", fallback=DEFAULTS["server"]["log_level"]).strip().upper(),
        name=parser.get("server", "name", fallback=DEFAULTS["server"]["name"]).strip() or "spacedesk-linux",
        in_flight_frames=get_int("server", "in_flight_frames"),
        width=get_int("capture", "width"),
        height=get_int("capture", "height"),
        jpeg_quality=get_int("capture", "jpeg_quality"),
        adaptive_quality=get_bool("capture", "adaptive_quality"),
        jpeg_quality_min=get_int("capture", "jpeg_quality_min"),
        jpeg_quality_max=get_int("capture", "jpeg_quality_max"),
        target_fps=get_int("capture", "target_fps"),
        keepalive_ms=get_int("capture", "keepalive_ms"),
        cursor_mode=get_int("capture", "cursor_mode"),
        source_type=get_int("capture", "source_type"),
        persist_permissions=get_bool("capture", "persist_permissions"),
        discovery_enabled=get_bool("discovery", "enabled"),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if not 1 <= settings.in_flight_frames <= 4:
        raise ConfigError(
            f"server.in_flight_frames fuera de rango (1-4): {settings.in_flight_frames}"
        )
    if not 1 <= settings.port <= 65535:
        raise ConfigError(f"server.port fuera de rango: {settings.port}")
    if len(settings.name.encode("utf-16-le")) > 256:
        raise ConfigError("server.name es demasiado largo (maximo 128 caracteres)")
    if settings.width <= 0 or settings.height <= 0:
        raise ConfigError(f"capture.width/height deben ser positivos: {settings.width}x{settings.height}")
    if not 1 <= settings.jpeg_quality <= 100:
        raise ConfigError(f"capture.jpeg_quality fuera de rango (1-100): {settings.jpeg_quality}")
    if not 1 <= settings.jpeg_quality_min <= 100:
        raise ConfigError(f"capture.jpeg_quality_min fuera de rango (1-100): {settings.jpeg_quality_min}")
    if not 1 <= settings.jpeg_quality_max <= 100:
        raise ConfigError(f"capture.jpeg_quality_max fuera de rango (1-100): {settings.jpeg_quality_max}")
    if settings.jpeg_quality_min > settings.jpeg_quality_max:
        raise ConfigError("capture.jpeg_quality_min no puede ser mayor que jpeg_quality_max")
    if settings.target_fps <= 0:
        raise ConfigError(f"capture.target_fps debe ser positivo: {settings.target_fps}")
    if settings.keepalive_ms < 0:
        raise ConfigError(f"capture.keepalive_ms no puede ser negativo: {settings.keepalive_ms}")
    if settings.cursor_mode not in CURSOR_MODES:
        raise ConfigError(f"capture.cursor_mode debe ser uno de {CURSOR_MODES}: {settings.cursor_mode}")
    if settings.source_type not in (SOURCE_MONITOR, SOURCE_VIRTUAL, SOURCE_MONITOR | SOURCE_VIRTUAL):
        raise ConfigError(
            f"capture.source_type debe ser 1 (monitor), 4 (virtual) o 5 (cualquiera): "
            f"{settings.source_type}"
        )