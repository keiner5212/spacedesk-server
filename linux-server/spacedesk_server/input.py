"""
Inyección de input vía `org.gnome.Mutter.RemoteDesktop.Session` (ver
`capture.py` para el por qué de este mecanismo en vez de python-evdev/uinput).

Las coordenadas de touch/mouse se mandan en el espacio del stream (0..width,
0..height de la captura, NO del escritorio combinado) -- Mutter las enruta al
monitor virtual correcto sin ambigüedad ni offset que calcular.

Se sigue usando `evdev.ecodes` como fuente de las constantes de keycode
(KEY_A, BTN_LEFT, etc.) porque son justamente los códigos evdev que
`NotifyKeyboardKeycode`/`NotifyPointerButton` esperan -- no porque se cree un
dispositivo uinput (ya no se usa `UInput`).

Mouse (manejo de botones) y Keyboard son implementaciones "best effort": el
layout de offsets está confirmado (ver protocol.py) pero el significado
exacto de algunos bits/códigos no se verificó contra el código fuente
decompilado. Documentado en cada función.
"""

import logging

from evdev import ecodes as e
from gi.repository import Gio, GLib

from .protocol import KeyboardPacket, MousePacket, TouchAction, TouchPacket

log = logging.getLogger("spacedesk.input")

REMOTEDESKTOP_BUS_NAME = "org.gnome.Mutter.RemoteDesktop"
REMOTEDESKTOP_SESSION_IFACE = "org.gnome.Mutter.RemoteDesktop.Session"

# Mapeo parcial de Android KeyEvent.KEYCODE_* (vkeycode) a evdev KEY_*.
# Cubre letras, números y teclas de control comunes. Incompleto a propósito:
# ampliar según se necesite, validando con adb logcat qué vkeycode manda la app
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
    """`monitor_width/height` son el tamaño del stream (espacio de
    coordenadas en el que `NotifyTouchDown`/`NotifyPointerMotionAbsolute`
    esperan recibir x/y). `conn` es la misma `Gio.DBusConnection` que usa
    `VirtualMonitorCapture` (se reusa, no se abre una nueva)."""

    def __init__(
        self,
        conn,
        remote_desktop_session_path: str,
        stream_path: str,
        monitor_width: int,
        monitor_height: int,
    ):
        self.conn = conn
        self.session_path = remote_desktop_session_path
        self.stream_path = stream_path
        self.screen_width = monitor_width
        self.screen_height = monitor_height
        self._mouse_buttons_down = 0

    def close(self) -> None:
        pass  # la sesion la cierra VirtualMonitorCapture.stop(), compartida entre clientes

    def _call(self, method: str, arg_variant) -> None:
        try:
            self.conn.call_sync(
                REMOTEDESKTOP_BUS_NAME, self.session_path, REMOTEDESKTOP_SESSION_IFACE,
                method, arg_variant, None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error:
            log.exception("Fallo llamando %s en la sesion RemoteDesktop", method)

    def _scale(self, x: int, y: int, res_x: int, res_y: int) -> tuple[float, float]:
        """Escala la coordenada del cliente (en su propio espacio res_x/res_y)
        al tamaño del stream."""
        if res_x <= 0 or res_y <= 0:
            sx, sy = x, y
        else:
            sx = x * self.screen_width / res_x
            sy = y * self.screen_height / res_y
        sx = max(0.0, min(self.screen_width - 1, sx))
        sy = max(0.0, min(self.screen_height - 1, sy))
        return sx, sy

    # -- Touch: alta confianza, layout y action codes confirmados (ver protocol.py) --
    def handle_touch(self, pkt: TouchPacket) -> None:
        x, y = self._scale(pkt.x, pkt.y, pkt.res_x, pkt.res_y)
        slot = pkt.pointer_id

        if pkt.action == TouchAction.DOWN:
            self._call("NotifyTouchDown", GLib.Variant("(sudd)", (self.stream_path, slot, x, y)))
        elif pkt.action == TouchAction.MOVE:
            self._call("NotifyTouchMotion", GLib.Variant("(sudd)", (self.stream_path, slot, x, y)))
        elif pkt.action == TouchAction.UP:
            self._call("NotifyTouchUp", GLib.Variant("(u)", (slot,)))
        else:
            log.debug("TouchAction desconocido: %s", pkt.action)

    # -- Mouse: posición absoluta confirmada; bits exactos de button_flags NO
    # confirmados contra código decompilado -- best effort, validar con logcat. --
    def handle_mouse(self, pkt: MousePacket) -> None:
        if pkt.wheel_delta:
            steps = 1 if pkt.wheel_delta > 0 else -1
            self._call("NotifyPointerAxisDiscrete", GLib.Variant("(ui)", (0, steps)))

        if pkt.x or pkt.y:
            x, y = self._scale(pkt.x, pkt.y, self.screen_width, self.screen_height)
            self._call("NotifyPointerMotionAbsolute", GLib.Variant("(sdd)", (self.stream_path, x, y)))

        # Heurística simple mientras no se confirme el bitmask real: cualquier
        # bit distinto de cero se interpreta como "botón izquierdo presionado",
        # y 0 como "soltado". Ver protocol.py para más detalle del gap.
        is_down = pkt.button_flags != 0
        was_down = self._mouse_buttons_down != 0
        if is_down != was_down:
            self._call("NotifyPointerButton", GLib.Variant("(ib)", (e.BTN_LEFT, is_down)))
        self._mouse_buttons_down = pkt.button_flags

    # -- Keyboard: layout de offsets visto en S1.java, distinción up/down NO
    # confirmada -- por ahora se trata todo evento como "tap" (down+up). --
    def handle_keyboard(self, pkt: KeyboardPacket) -> None:
        code = ANDROID_KEYCODE_TO_EVDEV.get(pkt.vkeycode)
        if code is None:
            log.debug("vkeycode sin mapeo: %s", pkt.vkeycode)
            return
        self._call("NotifyKeyboardKeycode", GLib.Variant("(ub)", (code, True)))
        self._call("NotifyKeyboardKeycode", GLib.Variant("(ub)", (code, False)))
