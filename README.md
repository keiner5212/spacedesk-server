# SpaceDesk Linux Server

Un servidor Linux que reimplementa el protocolo propietario de SpaceDesk, permitiendo usar una tablet Android como pantalla extendida del PC sin modificar la app oficial.

**Características:**
- ✅ Soporte por **USB** (Android Open Accessory / AOA) — sin red, cable directo
- ✅ Soporte por **WiFi** (TCP nativo + WebSocket para visor HTML5)
- ✅ Video fluido con JPEG optimizado por transporte (95 USB, 55 WiFi)
- ✅ Touch, mouse y teclado virtuales en tiempo real
- ✅ Monitor virtual real en GNOME/Wayland (RecordVirtual + RemoteDesktop)
- ✅ Captura de pantalla con GStreamer/PipeWire sin modificar el kernel

## Requisitos

- **OS**: Ubuntu 22.04+ o Debian con GNOME 42+ y Wayland
- **Python 3.10+**
- **Tablet Android**: app spacedesk oficial (no se modifica)

### Dependencias de sistema

```bash
sudo apt update
sudo apt install -y \
  python3-dev \
  python3-venv \
  python3-gi \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  libgstreamer1.0-0 \
  libgst-plugins-base1.0-0 \
  libpipewiregst-0.3-0 \
  libusb-1.0-0 \
  libusb-1.0-0-dev
```

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/spacedesk-linux-server.git
cd spacedesk-linux-server/linux-server
```

### 2. Crear entorno virtual con PyGObject del sistema

```bash
python3 -m venv .venv-usb --system-site-packages
source .venv-usb/bin/activate
```

### 3. Instalar dependencias Python

```bash
pip install --upgrade pip setuptools
pip install -r requirements.txt
```

## Uso

### Arrancar el servidor (con soporte USB)

```bash
cd linux-server
.venv-usb/bin/python main.py
```

El servidor escucha en puerto **28252** (TCP + UDP discovery).

### Conectar la tablet

**Por USB:**
1. Conectar tablet con cable USB (desbloqueada, con app spacedesk instalada)
2. El servidor hace el handshake AOA automáticamente
3. La app se lanza sola en modo USB display (si pregunta, marcar "Usar por defecto")

**Por WiFi:**
1. Tablet en la misma red LAN
2. La app descubre el servidor automáticamente (UDP broadcast en :28252)
3. O conectar manualmente por IP

## Configuración

### Permisos USB (sin sudo)

Si al conectar por USB ves errores de acceso, instalar la regla udev:

```bash
sudo cp linux-server/99-spacedesk-usb.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger
```

Luego desconectar/reconectar la tablet.

### Calibración de calidad JPEG

En `linux-server/spacedesk_server/server.py`, línea ~179:

```python
jpeg_quality = 95 if addr == "USB" else 55  # Ajustar según tu red
```

- **95-100**: USB (ancho de banda generoso, ~480 Mbps)
- **55-75**: WiFi (ancho de banda limitado)

## Arquitectura

```
linux-server/
├── main.py                         # Punto de entrada
├── spacedesk_server/
│   ├── protocol.py                # Header binario 128B + enums (protocolo spacedesk)
│   ├── capture.py                 # RecordVirtual + GStreamer/PipeWire → JPEG
│   ├── input.py                   # Touch/mouse/teclado virtuales vía RemoteDesktop D-Bus
│   ├── server.py                  # Servidor TCP/WebSocket + USB, manejo de conexiones
│   ├── usb_transport.py           # Android Open Accessory (AOA) handshake + lectura/escritura
│   ├── ws_transport.py            # Framing WebSocket (visor HTML5)
│   └── discovery.py               # Responde broadcast UDP "SPACEDESK-NET-CLIENT"
├── aoa_session.py                 # Script standalone para diagnóstico USB
├── 99-spacedesk-usb.rules         # Regla udev (sin sudo)
└── requirements.txt               # Dependencias pip
```

## Troubleshooting

### "Display off" o pantalla negra con "Connected"

✅ **Solucionado en USB**: la API `UsbAccessory` de Android entrega datos por transferencia USB completa. Si header+payload van concatenados en una sola escritura, el payload se pierde. **Fix**: `usb_transport.UsbConnection.write_packet()` manda header y payload en 2 escrituras independientes.

### Sesión USB muy corta o loop infinito tras reiniciar

✅ **Solucionado**: si el servidor se reinicia mientras la tablet está en accessory mode, el buffer USB tiene datos viejos. **Fix automático en `usb_acceptor_loop`**: si una sesión dura < 1s, se resetea el dispositivo USB antes del siguiente intento.

### Touch cae en monitor equivocado

🚧 **Pausado**: problema potencial de mapeo touch-a-monitor en GNOME. En USB esto típicamente no aplica (touch confirmado funcionando perfecto). Ver `ESTADO.md` para hipótesis de solución.

### "pyusb no está instalado"

Solo WiFi, sin USB. Para habilitar USB:
```bash
pip install pyusb
```

## Próximos pasos

1. ✅ **USB video + touch** — confirmado funcionando
2. Probar teclado explícitamente por USB (debería funcionar igual que WiFi)
3. Evaluar H264 de nuevo con ancho de banda USB generoso
4. Retomar mapeo touch-a-monitor si es prioritario

## Desarrollo / Diagnóstico

### Ver logs en tiempo real

```bash
.venv-usb/bin/python main.py 2>&1 | grep -E "ERROR|WARNING|Cliente"
```

### Test de handshake AOA solo

```bash
.venv-usb/bin/python aoa_session.py
```

Diagnostica:
- Detecta tablet en modo normal (339b:107d)
- Hace handshake AOA
- Lee el primer paquete Identification
- Muestra detalles de versión/compresión/resolución

### Decompilación de APK oficial

Se usó `jadx` (disponible en `https://github.com/skylot/jadx/releases`) para auditar:
- Strings AOA que la app espera (debe terminar en `" (spacedesk)"`)
- Formato exacto del protocolo de red
- Comportamiento real del receptor USB

## Referencias

- [Protocolo spacedesk](https://github.com/datronicsoft/SpaceDesk-protocol-documentation) (análisis inverso del HTML5 viewer)
- [Android Open Accessory protocol](https://developer.android.com/guide/topics/connectivity/usb/aoa)
- [RecordVirtual + RemoteDesktop D-Bus](https://gitlab.gnome.org/GNOME/mutter/-/blob/main/src/backends/meta-remote-desktop.c)
- [GStreamer + PipeWire](https://pipewire.org/)

## Licencia

MIT — Uso libre sin restricciones, con atribución.

## Autor

Desarrollado para usar la tablet HONOR NDL-W09 (6.7", 2388x1080) en Ubuntu 24.04 GNOME/Wayland.

---

¿Problemas? Abre un issue en GitHub o revisá `ESTADO.md` para investigación más profunda.
