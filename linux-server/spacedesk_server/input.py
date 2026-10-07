"""
Inyeccion de input via `org.freedesktop.portal.RemoteDesktop` (ver `capture.py`
para el por que del portal en vez de evdev/uinput).

Las coordenadas de touch/mouse van en el espacio logico del STREAM de
PipeWire (`size` de `streams[]`), no en el del escritorio combinado ni en el
del framebuffer enviado: son el tamano real del output virtual. El portal las
traduce al output correcto, sin ambigüedad ni offset que calcular.

`evdev.ecodes` se sigue usando como fuente de las constantes de keycode
(KEY_A, BTN_LEFT) porque son justamente los codigos evdev que
`NotifyKeyboardKeycode` / `NotifyPointerButton` esperan.

Mouse (botones) y Keyboard son implementaciones "best effort": el layout de
offsets esta confirmado (ver protocol.py) pero el significado exacto de
algunos bits no se verifico contra el cod fuente decompilado. Documentado en
cada funcion.
"""

import logging

from evdev import ecodes as e
from gi.repository import Gio, GLib

from .protocol import KeyboardPacket, MousePacket, TouchAction, TouchPacket

log = logging.getLogger("spacedesk.input")

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
REMOTEDESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"

# Mapeo parcial de Android KeyEvent.KEYCODE_* (vkeycode) a evdev KEY_*.
# Cubre letras, numeros y teclas de control comunes. Incompleto a proposito:
# ampliar segun se necesite, validando con adb logcat que vkeycode manda la app
# para cada tecla real.
ANDROID_KEYCODE_TO_EVDEV = {
    7: e.KEY_0, 8: e.KEY_1, 9: e.KEY_2, 10: e.KEY_3, 11: e.KEY_4,
    12: e.KEY_5, 13: e.KEY_6, 14: e.KEY_7, 15: e.KEY_8, 16: e.KEY_9,
    29: e.KEY_A, 30: e.KEY_B, 31: e.KEY_C, 32: e.KEY_D, 33: e.KEY_E,
    34: e.KEY_F, 35: e.KEY_G, 36: e.KEY_H, 37: e.KEY_I, 38: e.KEY_J,
    39: e.KEY_K, 40: e.KEY_L, 41: e.KEY_M, 42: e.KEY_N, 43: e.KEY_O,
    44: e.KEY_P, 45: e.KEY_Q, 46: e.KEY_R, 47: e.KEY_S, 48: e.KEY_T,
    49: e.KEY_U, 50: e.KEY_V, 51: e.KEY_W, 52: e.KEY_X, 53: e.KEY_Y,
    54: e.KEY_Z,
    19: e.KEY_UP, 20: e.KEY_DOWN, 21: e.KEY_LEFT, 22: e.KEY_RIGHT,
    61: e.KEY_TAB, 62: e.KEY_SPACE, 66: e.KEY_ENTER, 67: e.KEY_BACKSPACE,
    111: e.KEY_ESC, 59: e.KEY_LEFTSHIFT, 113: e.KEY_LEFTCTRL, 57: e.KEY_LEFTALT,
}


class VirtualInput:
    """`stream_width`/`stream_height` son el tamano logico del stream de
    PipeWire (el espacio en el que `NotifyTouchDown` /
    `NotifyPointerMotionAbsolute` esperan recibir x/y). `stream_node_id` es el
    `node_id` de PipeWire que devolvio `RemoteDesktop.Start`. `conn` es la
    misma `Gio.DBusConnection` que usa `VirtualMonitorCapture`."""

    def __init__(
        self,
        conn,
        session_handle: str,
        stream_node_id: int,
        stream_width: int,
        stream_height: int,
    ):
        self.conn = conn
        self.session_handle = session_handle
        self.stream_node_id = stream_node_id
        self.stream_width = stream_width
        self.stream_height = stream_height
        self._mouse_buttons_down = 0

    def close(self) -> None:
        pass  # la sesion la cierra VirtualMonitorCapture.stop(), compartida entre clientes

    def _call(self, method: str, signature: str, args: tuple) -> None:
        try:
            self.conn.call_sync(
                PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, REMOTEDESKTOP_IFACE,
                method, GLib.Variant(signature, (self.session_handle, {}, *args)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.warning("Fallo %s: %s", method, exc.message)

    def _scale(self, x: int, y: int, res_x: int, res_y: int) -> tuple[float, float]:
        """Escala la coordenada del cliente (en su propio espacio res_x/res_y)
        al tamano logico del stream."""
        if res_x <= 0 or res_y <= 0:
            sx, sy = x, y
        else:
            sx = x * self.stream_width / res_x
            sy = y * self.stream_height / res_y
        sx = max(0.0, min(self.stream_width - 1, sx))
        sy = max(0.0, min(self.stream_height - 1, sy))
        return sx, sy

    # -- Touch: alta confianza, layout y action codes confirmados (ver protocol.py) --
    def handle_touch(self, pkt: TouchPacket) -> None:
        x, y = self._scale(pkt.x, pkt.y, pkt.res_x, pkt.res_y)
        slot = pkt.pointer_id

        if pkt.action == TouchAction.DOWN:
            self._call("NotifyTouchDown", "(oa{sv}uudd)",
                       (self.stream_node_id, slot, x, y))
        elif pkt.action == TouchAction.MOVE:
            self._call("NotifyTouchMotion", "(oa{sv}uudd)",
                       (self.stream_node_id, slot, x, y))
        elif pkt.action == TouchAction.UP:
            self._call("NotifyTouchUp", "(oa{sv}u)", (slot,))
        else:
            log.debug("TouchAction desconocido: %s", pkt.action)

    # -- Mouse: posicion absoluta confirmada; bits exactos de button_flags NO
    # confirmados contra codigo decompilado -- best effort, validar con logcat. --
    def handle_mouse(self, pkt: MousePacket) -> None:
        if pkt.wheel_delta:
            steps = 1 if pkt.wheel_delta > 0 else -1
            self._call("NotifyPointerAxisDiscrete", "(oa{sv}ui)", (0, steps))

        if pkt.x or pkt.y:
            x, y = self._scale(pkt.x, pkt.y, self.stream_width, self.stream_height)
            self._call("NotifyPointerMotionAbsolute", "(oa{sv}udd)",
                       (self.stream_node_id, x, y))

        # Heuristica simple mientras no se confirme el bitmask real: cualquier
        # bit distinto de cero se interpreta como "boton izquierdo presionado",
        # y 0 como "soltado". Ver protocol.py para mas detalle del gap.
        is_down = pkt.button_flags != 0
        was_down = self._mouse_buttons_down != 0
        if is_down != was_down:
            self._call("NotifyPointerButton", "(oa{sv}iu)", (e.BTN_LEFT, 1 if is_down else 0))
        self._mouse_buttons_down = pkt.button_flags

    # -- Keyboard: layout de offsets visto en S1.java, distincion up/down NO
    # confirmada -- por ahora se trata todo evento como "tap" (down+up). --
    def handle_keyboard(self, pkt: KeyboardPacket) -> None:
        code = ANDROID_KEYCODE_TO_EVDEV.get(pkt.vkeycode)
        if code is None:
            log.debug("vkeycode sin mapeo: %s", pkt.vkeycode)
            return
        self._call("NotifyKeyboardKeycode", "(oa{sv}iu)", (code, 1))
        self._call("NotifyKeyboardKeycode", "(oa{sv}iu)", (code, 0))