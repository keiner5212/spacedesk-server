# SpaceDesk Linux Server

Un servidor Linux que reimplementa el protocolo propietario de SpaceDesk, permitiendo usar una tablet Android como pantalla extendida del PC sin modificar la app oficial.

**Características:**
- Video fluido con JPEG configurable
- Touch, mouse y teclado virtuales en tiempo real
- Monitor virtual real de KWin (Plasma 6) via el portal XDG ScreenCast
- Captura con GStreamer/PipeWire, sin modulos de kernel ni root
- Configuracion por archivo (`config.ini`), arranque con un script

## Requisitos

- **OS**: Debian/Ubuntu con **KDE Plasma 6+ en Wayland** (tambien funciona en GNOME 42+, el portal es el mismo)
- **Python 3.10+**
- **Tablet Android**: app spacedesk oficial (no se modifica)

### Dependencias de sistema

```bash
sudo apt update
sudo apt install -y \
  python3-venv \
  python3-gi \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  libgstreamer1.0-0 \
  libgst-plugins-base1.0-0 \
  libpipewiregst-0.3-0 \
  python3-evdev
```

En Plasma ya viene `xdg-desktop-portal-kde` (el backend del portal ScreenCast/RemoteDesktop). Verificalo con:

```bash
busctl --user get-property org.freedesktop.portal.Desktop \
  /org/freedesktop/portal/desktop org.freedesktop.portal.ScreenCast AvailableSourceTypes
```

Tiene que devolver **7** (1=MONITOR, 2=WINDOW, 4=VIRTUAL). Con 3 no hay monitor
virtual y solo se puede espejar un monitor existente.

## Instalación

```bash
git clone https://github.com/tu-usuario/spacedesk-linux-server.git
cd spacedesk-linux-server/linux-server
./start.sh --setup
```

`start.sh` crea el venv `.venv` con `--system-site-packages` (PyGObject,
GStreamer y python-evdev vienen del sistema, no se instalan por pip) e instala
`requirements.txt`.

## Uso

```bash
./start.sh                     # usa config.ini
./start.sh --config otro.ini   # otro archivo de configuracion
```

El servidor escucha en el puerto **28252** (TCP + UDP discovery). El monitor
virtual se crea la primera vez que se conecta la tablet: en ese momento
aparece el dialogo de permisos de KDE para compartir la pantalla y el
dispositivo de entrada. Se acepta una vez y queda guardado para los proximos
arranques.

### Conectar la tablet

1. Tablet en la misma red LAN.
2. La app descubre el servidor automaticamente (UDP broadcast en :28252), o se
   puede poner la IP a mano.
3. Al conectar, en "Configuracion de pantalla" de KDE deberia aparecer un
   monitor virtual nuevo: se le pueden mover ventanas como a uno fisico.

## Configuracion

Todo esta en `config.ini`:

| Opcion | Default | Que hace |
| --- | --- | --- |
| `server.port` | 28252 | Puerto de datos y discovery |
| `server.log_level` | INFO | DEBUG / INFO / WARNING / ERROR |
| `capture.width` / `capture.height` | 1920 / 1200 | Framebuffer que ve la tablet (1:1, sin escalar) |
| `capture.jpeg_quality` | 55 | Calidad JPEG. 55-75 WiFi, 90-100 red muy rapida |
| `capture.cursor_mode` | 2 | 1 oculto, 2 dibujado en el frame, 4 metadata |
| `capture.source_type` | 4 | 4 monitor virtual, 1 espeja un monitor fisico, 5 elegir |
| `capture.persist_permissions` | true | Recordar el dialogo de permisos entre arranques |
| `discovery.enabled` | true | Responder al broadcast UDP de la app |

## Arquitectura

```
linux-server/
├── main.py                         # Punto de entrada (--config)
├── start.sh                        # Crea el venv y arranca el servidor
├── config.ini                      # Configuracion
├── spacedesk_server/
│   ├── config.py                   # Lectura y validacion de config.ini
│   ├── protocol.py                 # Header binario 128B + enums (protocolo spacedesk)
│   ├── capture.py                  # Portal ScreenCast(VIRTUAL) + RemoteDesktop + GStreamer -> JPEG
│   ├── input.py                    # Touch/mouse/teclado via RemoteDesktop del portal
│   ├── server.py                   # Servidor TCP + WebSocket, manejo de conexiones
│   ├── ws_transport.py             # Framing WebSocket (visor HTML5)
│   └── discovery.py                # Responde broadcast UDP "SPACEDESK-NET-CLIENT"
└── requirements.txt
```

Como funciona la captura:

1. `RemoteDesktop.CreateSession` -> sesion del portal.
2. `SelectDevices` (teclado + puntero + touch) y `ScreenCast.SelectSources`
   con `types=VIRTUAL`: KWin crea un output virtual real.
3. `RemoteDesktop.Start`: el usuario aprueba el dialogo; devuelve el `node_id`
   de PipeWire del stream.
4. `ScreenCast.OpenPipeWireRemote`: file descriptor de PipeWire.
5. Pipeline GStreamer `pipewiresrc -> videoconvert -> videoscale -> jpegenc -> appsink`.

El input se manda por las mismas llamadas `NotifyTouchDown` /
`NotifyPointerMotionAbsolute`, pero con el `node_id` de PipeWire en vez de un
path D-Bus, y en el espacio logico del stream (no en el del escritorio
combinado). El portal lo traduce al monitor virtual correcto.

## Troubleshooting

### `org.gnome.Mutter.ScreenCast: ServiceUnknown`

Ya no debería aparecer: el código usa los portales XDG. Si aparece es que se
está ejecutando una versión vieja del servidor. La causa raíz histórica está en
`ESTADO.md`.

### `No se pudo crear la captura: ...` en el log

Una sola línea, una sola vez. El servidor sigue escuchando pero rechaza a los
clientes con un mensaje corto en vez de un traceback por reconexión. Causas
típicas:

- No estás en una sesión Wayland (una sesión X11 pura no tiene portal de
  pantalla).
- `AvailableSourceTypes` no incluye el bit 4 (VIRTUAL): no hay monitor virtual
  disponible; pon `source_type = 1` para espejar un monitor existente.
- El dialogo de permisos se cancela o expira (180 s sin responder).

### El dialogo de permisos aparece en cada arranque

`capture.persist_permissions` no pudo persistir el permiso en este build de
`xdg-desktop-portal-kde`. Es cosmético: el flujo funciona igual, solo hay que
aceptar el dialogo una vez más.

### Pantalla negra con "Connected"

- Verificar que el monitor virtual aparece en la configuracion de pantallas de
  KDE. Si no aparece, el portal no creo el output: mirar el log.
- `log_level = DEBUG` para ver las caps reales del primer frame.
- Bajar `capture.jpeg_quality` si la imagen sale a medias.

### Touch que no responde

Mirar la linea `Dispositivos de input otorgados:` en el log. Si el numero no es
`7` (KEYBOARD|POINTER|TOUCHSCREEN = 1|2|4), al portal no le otorgaron todos los
dispositivos y hay que aceptarlos de nuevo en el dialogo.

## Diagnóstico

```bash
./start.sh 2>&1 | grep -E "ERROR|WARNING|Captura|Stream"
```

Capturas que ver en los logs al arrancar bien:

```
Sesion RemoteDesktop creada: /org/freedesktop/portal/desktop/session/...
Dispositivos de input otorgados: 7
Stream ... (node_id=42) logico=1920x1080, fuente=4
Pipeline de captura iniciado (1920x1200, jpeg=55)
Caps reales del primer frame capturado: video/x-raw,format=I420,...
```

## Referencias

- [Protocolo spacedesk](https://github.com/datronicsoft/SpaceDesk-protocol-documentation)
- [org.freedesktop.portal.ScreenCast](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [org.freedesktop.portal.RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- [GStreamer + PipeWire](https://pipewire.org/)

## Licencia

MIT - Uso libre sin restricciones, con atribución.

## Autor

Desarrollado para usar la tablet HONOR NDL-W09 (6.7", 2388x1080) en Debian con
KDE Plasma 6 / Wayland.

---

¿Problemas? Revisa `ESTADO.md` para la historia de la investigacion.