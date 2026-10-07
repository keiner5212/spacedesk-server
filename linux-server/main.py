#!/usr/bin/env python3
"""
Punto de entrada del servidor spacedesk para Linux.

Usa el Python del sistema (/usr/bin/python3) a traves del venv de start.sh:
necesita los bindings PyGObject (gi) del sistema para GStreamer/D-Bus, que no
se pueden instalar por pip. La configuracion se lee de config.ini.

Ejecutar: ./start.sh  (o python main.py --config otro.ini)
"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spacedesk_server import config as config_module  # noqa: E402
from spacedesk_server.server import main  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Servidor spacedesk para Linux")
    parser.add_argument(
        "--config",
        default=None,
        help="ruta del archivo INI de configuracion (por defecto config.ini junto a main.py)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log DEBUG: muestra cada paquete de input y el estado del pipeline",
    )
    args = parser.parse_args()

    try:
        settings = config_module.load(args.config)
    except config_module.ConfigError as error:
        parser.exit(2, f"Error de configuracion: {error}\n")

    if args.debug:
        settings = replace(settings, log_level="DEBUG")

    main(settings)