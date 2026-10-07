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
    },
    "capture": {
        "width": "1920",
        "height": "1200",
        "jpeg_quality": "55",
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
    width: int
    height: int
    jpeg_quality: int
    cursor_mode: int
    source_type: int
    persist_permissions: bool
    discovery_enabled: bool

    def describe(self) -> str:
        return (
            f"puerto={self.port} framebuffer={self.width}x{self.height} "
            f"jpeg={self.jpeg_quality} fuente={self.source_type} "
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
        width=get_int("capture", "width"),
        height=get_int("capture", "height"),
        jpeg_quality=get_int("capture", "jpeg_quality"),
        cursor_mode=get_int("capture", "cursor_mode"),
        source_type=get_int("capture", "source_type"),
        persist_permissions=get_bool("capture", "persist_permissions"),
        discovery_enabled=get_bool("discovery", "enabled"),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if not 1 <= settings.port <= 65535:
        raise ConfigError(f"server.port fuera de rango: {settings.port}")
    if settings.width <= 0 or settings.height <= 0:
        raise ConfigError(f"capture.width/height deben ser positivos: {settings.width}x{settings.height}")
    if not 1 <= settings.jpeg_quality <= 100:
        raise ConfigError(f"capture.jpeg_quality fuera de rango (1-100): {settings.jpeg_quality}")
    if settings.cursor_mode not in CURSOR_MODES:
        raise ConfigError(f"capture.cursor_mode debe ser uno de {CURSOR_MODES}: {settings.cursor_mode}")
    if settings.source_type not in (SOURCE_MONITOR, SOURCE_VIRTUAL, SOURCE_MONITOR | SOURCE_VIRTUAL):
        raise ConfigError(
            f"capture.source_type debe ser 1 (monitor), 4 (virtual) o 5 (cualquiera): "
            f"{settings.source_type}"
        )