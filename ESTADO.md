# SpaceDesk Linux Server — Estado del proyecto

## 2026-10-06: migración de GNOME/Mutter a KDE Plasma + portales XDG

**Síntoma**: al conectar la tablet, cada intento de captura fallaba con
`GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name
org.gnome.Mutter.RemoteDesktop was not provided by any .service files (2)`,
repetido ~6 veces por segundo (la tablet reconecta en loop y cada intento
volcaba un traceback completo).

**Causa raíz: el escritorio cambió, el código no**. `capture.py`/`input.py`
estaban escritos solo contra la API privada de Mutter
(`org.gnome.Mutter.ScreenCast.RecordVirtual` + `org.gnome.Mutter.RemoteDesktop`).
Esta máquina corre **KDE Plasma 6.7.4 en Wayland** (`kwin_wayland` es dueño de
`wayland-0`; `XDG_CURRENT_DESKTOP=XFCE` es solo el entorno de sesión), y Mutter
no está instalado ni corre. No hay ningún nombre `org.gnome.Mutter.*` en el bus
de sesión, así que la API no podía funcionar bajo ninguna circunstancia. La
sección "Arquitectura" de más abajo describe el diseño sobre Ubuntu 24.04 +
GNOME; esta máquina es Debian sid + Plasma.

**Qué se cambió**:
- `capture.py`: reescrito sobre `org.freedesktop.portal.ScreenCast` con
  `types=VIRTUAL` + `org.freedesktop.portal.RemoteDesktop` sobre la misma sesión.
  En este build `AvailableSourceTypes` = **7** (MONITOR|WINDOW|VIRTUAL), así que
  el portal de KDE puede crear un output virtual real de KWin, no una superficie
  de screencast aislada.
- `input.py`: mismas llamadas `Notify*`, pero con el `node_id` de PipeWire
  (`u`) en vez del path D-Bus del stream (`s`), y escalando al tamaño **lógico
  del stream** (el `size` de `streams[]`), que es el espacio de coordenadas que
  espera el portal.
- `server.py`: `SharedCapture` recuerda el fallo de captura. Antes cada
  reconexión de la tablet reintentaba el portal y volcaba un traceback; ahora se
  loguea **una** vez y cada cliente se rechaza con una línea corta.
- `config.py` + `config.ini`: puerto, nivel de log, resolución, calidad JPEG,
  modo del cursor, tipo de fuente del portal y persistencia de permisos.
- `start.sh`: crea el venv (`.venv`, `--system-site-packages`) y arranca el
  servidor. `main.py --config <ruta>` para otro INI.
- **Eliminado el transporte USB** (pedido explícito): `usb_transport.py`,
  `aoa_session.py`, `aoa_test.py`, `99-spacedesk-usb.rules` y la dependencia
  `pyusb`. La tablet se conecta por WiFi. Por eso también se fue la calidad JPEG
  diferenciada 95/USB vs 55/WiFi: ahora hay una sola en `config.ini`.

**Pendiente de verificar en la máquina** (requiere la tablet y aceptar el
diálogo de permisos de KDE):
1. Que el output virtual aparece en la configuración de pantallas de KDE.
2. Que `capture.persist_permissions` evita el diálogo en el segundo arranque
   (`xdg-desktop-portal-kde` tiene historial de implementar esto a medias).
3. Que `granted_devices` = 7 (teclado + puntero + touch) en el log.
4. Touch sobre el monitor virtual (el portal traduce las coordenadas del
   stream al output correcto, igual que hacía Mutter).

**Limitación conocida**: Plasma 6.7 crea el output virtual a **1920x1080 fijo**
(hardcodeado en `WaylandIntegration::startStreamingVirtual`); el `videoscale` del
pipeline GStreamer lo reescala al tamaño de `config.ini`, así que la tablet
sigue recibiendo 1920x1200. El input se escala al tamaño lógico del stream
(1920x1080), no al del framebuffer.

Objetivo: usar la tablet Android (app oficial spacedesk, sin modificar) como
pantalla extendida de este PC Linux (Ubuntu 24.04, GNOME/Wayland), mediante un
servidor propio que reimplementa el protocolo de red de spacedesk (que solo
tiene servidor oficial para Windows).

Tablet del usuario: 6.7", HD+ **2388x1080 px**, 90 Hz.

## Cómo arrancar

Con soporte USB (recomendado -- venv con PyGObject del sistema + pyusb):
```
cd "/home/leandro/Vídeos/SpaceDesk/linux-server"
.venv-usb/bin/python main.py
```

Solo WiFi/WebSocket, sin USB (no requiere el venv):
```
cd "/home/leandro/Vídeos/SpaceDesk/linux-server"
/usr/bin/python3 main.py
```

Importante: nunca usar el `python3` del PATH (es el de Anaconda, sin `gi`).
`.venv-usb` se creó con `--system-site-packages` así que tiene PyGObject/
GStreamer del sistema *y* pyusb -- es un superset de `/usr/bin/python3`, no
un reemplazo aislado. Si pyusb no está disponible, el servidor lo detecta y
sigue funcionando solo por WiFi (un log.warning, nada se rompe).

Por WiFi: la tablet debe estar en la misma LAN; la app debería detectar el
servidor solo (discovery UDP en :28252) o se puede poner la IP manualmente.
Por USB: conectar la tablet con el cable, desbloqueada, con la app
spacedesk abierta -- el servidor hace el handshake AOA solo y la sesión
arranca sin tocar nada en la tablet (ver sección USB abajo).

## Arquitectura

```
linux-server/
  main.py
  aoa_test.py       # script standalone de diagnostico: solo el handshake AOA
  aoa_session.py    # script standalone de diagnostico: handshake + lee el primer Identification
  spacedesk_server/
    protocol.py      # header binario 128B, enums, pack/unpack (offsets auditados contra el APK/JS oficial)
    capture.py        # monitor virtual de Mutter (org.gnome.Mutter.ScreenCast.RecordVirtual) + GStreamer/PipeWire -> JPEG
    input.py           # touch/mouse/teclado virtuales vía org.gnome.Mutter.RemoteDesktop (D-Bus)
    server.py          # servidor TCP+WebSocket :28252 + acceptor USB, handshake, loop FrameBuffer/ACK (handle_connection, compartido por los 3 transportes)
    usb_transport.py   # transporte Android Open Accessory (AOA): handshake, UsbConnection (misma interfaz que Connection)
    ws_transport.py    # framing WebSocket manual (visor HTML5)
    discovery.py        # responde broadcast UDP "SPACEDESK-NET-CLIENT"
```

Sin vkms, sin diálogos de portal: `RecordVirtual` crea un monitor virtual real
reconocido por Mutter (aparece como `Virtual-1`), sin que el usuario toque nada.

## USB (Android Open Accessory) — implementado y funcionando de punta a punta

El servidor ahora acepta conexiones por **tres transportes simultáneos**, todos
compartiendo la misma `SharedCapture` (es "la" misma pantalla extendida sin
importar por dónde se conecte): TCP crudo (app nativa por WiFi), WebSocket
(visor HTML5), y **USB** (cable, sin red).

Para USB: conectar la tablet con el cable, **desbloqueada**, con la app
spacedesk instalada (no hace falta tenerla abierta — el servidor hace todo el
handshake AOA solo y la app se auto-lanza). El usuario debe haber aceptado una
vez el diálogo "¿Abrir con spacedesk?" → "Usar por defecto" la primera vez que
Android lo pregunta; después es automático.

**Bug real encontrado y arreglado (la causa de que la pantalla quedara negra
con "Connected" pero sin imagen)**: la API `UsbAccessory` de Android entrega
los datos al `FileInputStream` del lado app **por transferencia USB completa**,
no como un stream continuo bufferizado. Si el servidor manda el header (128B)
y el payload JPEG **concatenados en una sola escritura USB**, esa transferencia
llega como una unidad; cuando la app pide leer solo 128 bytes (el header), esa
lectura consume **toda la transferencia** y el resto del payload se pierde
(no queda bufferizado para la siguiente llamada a `read()`). El hilo receptor
de la app (`SATaskLoopedFrameBufferProcessorUsb`) se queda entonces esperando
indefinidamente datos que ya se descartaron — confirmado con `adb logcat`
(tag `SA_USB`): el log se trababa para siempre en
`"OnExecute - getting packet"`, sin error ni progreso.

**Fix**: `UsbConnection.write_packet()` (en `usb_transport.py`) manda el
header y el payload como **dos escrituras bulk independientes**, nunca
concatenadas. Con esto el ciclo `FrameBuffer → FlowControlAck` funciona de
forma continua y fluida, confirmado con payloads de hasta 46KB+ (mayores al
límite de 16KB del buffer del USB gadget driver de Android — el loop de
lectura interno de la app sí maneja bien múltiples iteraciones dentro de UNA
transferencia lógica, el problema era solo mezclar header+payload).

**Otros hallazgos de esta sesión**:
- La `SurfaceView` donde la app dibuja es **siempre 1920x1200**, sin importar
  lo que el cliente reporte en su `Identification` (por WiFi reporta 1920x1200,
  por USB reporta 1920x1080 — inconsistencia propia de la app). Confirmado con
  logcat real (`addSurfaceChangedCallback ... 0,0-1920,1200`). `server.py`
  ahora ignora `ident.effective_width/height()` y usa siempre `(1920, 1200)`.
- Calidad JPEG diferenciada por transporte: **95** para USB (ancho de banda
  generoso, ~480Mbps bulk USB2 -- se probó 85/95/100 con el usuario; 95 y 100
  se ven igual de nítidos pero 95 pesa notablemente menos, así que es el punto
  óptimo), 55 para WiFi (ya afinado en sesión anterior por el cuello de
  botella real de transmisión). Ver `jpeg_quality` en `handle_connection`.
- **Touch confirmado funcionando perfecto por USB** (mismo código que WiFi,
  sin cambios necesarios).
- Reinicio en caliente del servidor con la tablet ya en accessory mode: el
  buffer USB puede tener datos viejos pendientes (touch events de la sesión
  anterior que nadie leyó), causando que el primer paquete leído no sea
  Identification y la sesión se cierre casi instantáneamente -- sin fix esto
  entra en un loop muy rápido reintentando sobre el mismo buffer sucio.
  **Fix**: en `usb_acceptor_loop`, si una sesión dura menos de 1s, resetear el
  dispositivo USB (`dev.reset()`) antes del próximo intento.
- El daemon `adb` del host, si está corriendo, "upgradea" agresivamente
  cualquier tablet Android conectada a modo `accessory+adb` (PID `18d1:2d01`)
  apenas la detecta — esto es inofensivo (ambos modos tienen los mismos
  endpoints 0x81/0x01 en la interfaz 0, así que el servidor funciona igual),
  pero **mientras eso sucede `dev.set_configuration()` puede fallar con
  "Resource busy"** si se llama incondicionalmente. Fix en
  `usb_transport.wait_for_accessory()`: solo llamar `set_configuration()` si
  el dispositivo no tiene ya una configuración activa.
- La tablet **revierte sola de accessory mode a modo normal** si nadie
  completa la sesión rápido (pocos segundos) — el `usb_acceptor_loop` ya
  maneja esto reintentando el handshake automáticamente en loop.

Ver memoria del proyecto (`spacedesk_servidor_implementacion.md`) para el
detalle completo del handshake AOA, los strings exactos, y la regla udev.

## Resuelto en esta sesión

1. **"Connected - display off"** → fix: mandar `Visibility(True)` justo después
   del handshake (`protocol.build_visibility_header`, en `server.py`).
2. **"Bandwidth is low"** → fix: keep-alive más agresivo, repetir el último
   frame cada 0.2s (antes 1.0s) cuando la pantalla está estática.
3. **Tamaño de imagen incorrecto** — **superado en la sesión de USB** (ver
   sección USB más arriba): se confirmó con `adb logcat` que la `SurfaceView`
   real de la app es **siempre 1920x1200** sin importar lo que reporte en su
   `Identification`. El código actual usa ese tamaño fijo directamente (no el
   cálculo de aspect ratio `height≈868` que se había planteado antes, nunca
   confirmado). Funciona bien tanto por WiFi como por USB; puede haber
   letterbox/pillarbox dado que la pantalla real (`2388x1080`, aspect 2.21:1)
   no coincide exactamente con 1920x1200 (aspect 1.6:1), pero no se reportó
   como distorsión — solo afectaba la nitidez/compresión, ya resuelto subiendo
   `jpeg_quality`.
4. **Cursor del mouse invisible**: se agregó `cursor-mode=2` (EMBEDDED, misma
   convención que el portal XDG `org.freedesktop.portal.ScreenCast`) a las
   propiedades de `RecordVirtual` en `capture.py`, para que Mutter dibuje el
   cursor directamente en los frames capturados. Mutter aceptó la propiedad
   sin error. **Pendiente de confirmar visualmente.**

## Investigado y descartado en esta sesión: mapeo touch/mouse al monitor correcto

Síntoma reportado por el usuario: al tocar la tablet (que ve el monitor
virtual), la "selección"/click ocurre en un monitor físico distinto del PC.

**Intento de solución (descartado)**: hacer que el touchscreen/mouse virtual
(creados con python-evdev en `input.py`) cubrieran todo el escritorio
combinado (todos los monitores reales + el virtual, según su posición real
consultada vía `org.gnome.Mutter.DisplayConfig.GetCurrentState`), traduciendo
las coordenadas del cliente sumando el offset del monitor virtual dentro de
ese escritorio.

**Por qué se descartó**: la consulta de la posición del monitor virtual
recién creado vía `GetCurrentState` resultó **no determinística**:
- A veces el conector `Virtual-1` aparecía casi inmediatamente, pero con una
  posición que no correspondía a la sesión actual (sospecha de datos
  obsoletos / sesión zombie de pruebas anteriores que Mutter no había
  liberado).
- Otras veces, con reintentos durante 3 segundos completos, el conector
  `Virtual-1` **nunca apareció** en `GetCurrentState`, a pesar de que
  `RecordVirtual` ya había devuelto éxito y PipeWire ya estaba entregando
  frames reales (confirmado con logging de diagnóstico que mostró solo
  `['eDP-1', 'HDMI-1']` durante toda la ventana de reintento).
- Se probó tanto reusar la conexión D-Bus existente como abrir una conexión
  D-Bus nueva exclusiva para la consulta — sin diferencia.
- Conclusión: el monitor virtual creado por `RecordVirtual` no se refleja de
  forma confiable/inmediata en `DisplayConfig.GetCurrentState`. Este enfoque
  se revirtió completamente (`capture.py` ya no intenta detectar posición;
  `offset_x=0`, `offset_y=0`, `desktop_width=width`, `desktop_height=height`
  fijos, comportamiento original).

**Hipótesis no probada para la próxima sesión**: la causa raíz de que el
touch caiga en el monitor físico equivocado es probablemente que GNOME manda
los eventos de dispositivos `INPUT_PROP_DIRECT` (touchscreens/tablets) al
**monitor primario** por defecto cuando no hay una asociación explícita
configurada — y `eDP-1` (pantalla del laptop) es el monitor primario, no el
virtual. Dos caminos no explorados todavía:
1. Configurar explícitamente la asociación dispositivo→monitor vía el schema
   relocatable `org.gnome.desktop.peripherals.touchscreen` (gsettings/dconf).
   Se confirmó que el schema existe y es relocatable, pero `dconf list
   /org/gnome/desktop/peripherals/touchscreens/` no devuelve ninguna entrada
   (GNOME no genera una entrada automáticamente solo por conectar el
   dispositivo evdev — requiere derivar el path manualmente, no determinado
   aún cómo).
2. Marcar temporalmente el monitor virtual como primario mientras el servidor
   esté activo (con `ApplyMonitorsConfig`), revirtiendo al cerrar. Tiene como
   efecto secundario que la barra superior/dock de GNOME se movería al
   monitor virtual mientras el servidor esté activo — molesto para el uso
   normal del PC, evaluar si vale la pena.

El usuario bajó la prioridad de este problema ("no me importa eso realmente")
frente al tamaño de imagen y la visibilidad del cursor, así que quedó en
pausa a propósito.

## Gaps de protocolo ya documentados (sin cambios, ver memoria del proyecto)

- Bitmask exacto de botones de `Mouse`: best-effort, no confirmado contra
  código fuente.
- Distinción key-up/key-down en `Keyboard`: gap abierto.
- Payload de nombre de dispositivo en `Identification`: no implementado.

## Próximos pasos sugeridos (en orden)

1. **USB por video+touch ya está confirmado funcionando de punta a punta** --
   video con calidad 95 (óptimo), touch perfecto. No queda nada pendiente del
   camino principal de USB.
2. Probar teclado por USB explícitamente (no se probó en esta sesión, mismo
   código que WiFi, debería funcionar igual).
3. Si se quiere apurar aún más la fluidez por USB (ya con buena calidad),
   evaluar subir el framerate de keep-alive o probar H264 de nuevo ahora que
   el cuello de botella de transmisión es mucho menor que WiFi (aunque H264
   ya se descartó por incompatibilidad de decoder, no por ancho de banda).
4. Retomar el mapeo touch-a-monitor (gsettings o monitor primario temporal)
   si el usuario lo vuelve a priorizar (quedó en pausa, ver sección abajo) --
   nota: con USB esto probablemente no aplica (no hay mapeo touch-a-monitor
   físico involucrado, el touch ya confirmó funcionar perfecto).
5. Evaluar si vale la pena distinguir mejor "TCP crudo" de "WebSocket" en el
   string `addr` que llega a `handle_connection` (hoy es una tupla de socket
   para ambos, y `"USB"` para USB) si en el futuro se quiere lógica específica
   por transporte más allá de la calidad JPEG.
