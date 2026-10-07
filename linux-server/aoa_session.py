#!/usr/bin/env python3
"""
Sesion AOA completa en un solo proceso (evita que la tablet revierta a modo
normal por el hueco entre scripts):

  1. Si esta en modo normal, hace el handshake AOA y espera la re-enumeracion.
  2. Abre el accessory device, reclama la interfaz 0.
  3. Escucha el endpoint IN (0x81) esperando el primer paquete del protocolo
     spacedesk (header 128B). spacedesk, como handler por defecto, deberia
     auto-lanzarse en modo USB y mandar su Identification.

No escribe nada todavia: solo diagnostica que la app habla por USB.
"""
import sys
import time
import struct
import usb.core
import usb.util

NORMAL_VID, NORMAL_PID = 0x339b, 0x107d
AOA_VID = 0x18d1
AOA_PIDS = [0x2d00, 0x2d01, 0x2d04, 0x2d05]
EP_IN, EP_OUT = 0x81, 0x01

ACCESSORY_GET_PROTOCOL = 51
ACCESSORY_SEND_STRING = 52
ACCESSORY_START = 53
STRINGS = {
    # IMPORTANTE: la app filtra el accesorio por getDescription().endsWith(" (spacedesk)")
    # (ver SAActivityDisplayUsb.i1()). El string DEBE terminar en " (spacedesk)".
    0: "datronicsoft", 1: "spacedesk", 2: "Linux PC (spacedesk)",
    3: "1.0", 4: "https://www.spacedesk.net", 5: "0000000012345678",
}
TYPE_NAMES = {0: "Identification", 1: "Ping", 2: "FrameBuffer", 3: "Visibility",
              7: "FlowControlAck", 8: "Disconnect", 10: "Mouse",
              11: "Keyboard", 12: "Touch"}


def find_accessory():
    for pid in AOA_PIDS:
        d = usb.core.find(idVendor=AOA_VID, idProduct=pid)
        if d:
            return d, pid
    return None, None


def do_handshake():
    dev = usb.core.find(idVendor=NORMAL_VID, idProduct=NORMAL_PID)
    if dev is None:
        return False
    print(f"[ok] Tablet en modo normal {NORMAL_VID:04x}:{NORMAL_PID:04x}, haciendo handshake AOA")
    ret = dev.ctrl_transfer(0xC0, ACCESSORY_GET_PROTOCOL, 0, 0, 2, timeout=2000)
    proto = ret[0] | (ret[1] << 8)
    print(f"[ok] AOA version {proto}")
    for idx, val in STRINGS.items():
        dev.ctrl_transfer(0x40, ACCESSORY_SEND_STRING, 0, idx, val.encode() + b"\x00", timeout=2000)
    dev.ctrl_transfer(0x40, ACCESSORY_START, 0, 0, None, timeout=2000)
    print("[ok] ACCESSORY_START enviado, esperando re-enumeracion...")
    usb.util.dispose_resources(dev)
    return True


def force_normal_mode():
    """Si la tablet quedo en accessory mode de una corrida anterior, la
    reseteamos para que vuelva a modo normal y poder rehacer el handshake
    con los strings actuales."""
    dev, pid = find_accessory()
    if dev is None:
        return
    print("[i] Quedo en accessory mode de antes; reseteando a modo normal...")
    try:
        dev.reset()
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)
    for _ in range(20):
        time.sleep(0.5)
        if usb.core.find(idVendor=NORMAL_VID, idProduct=NORMAL_PID) is not None:
            print("[ok] De vuelta en modo normal.")
            return
        if find_accessory()[0] is None:
            time.sleep(1.0)
    print("[i] (sigo de todos modos)")


def main():
    force_normal_mode()
    # Siempre hacemos un handshake fresco con los strings actuales
    if not do_handshake():
        # quiza ya estaba en accessory tras un reset que no revirtio
        dev, pid = find_accessory()
        if dev is None:
            print("[!] No encuentro la tablet. Conectala y desbloquea.")
            sys.exit(1)
    dev, pid = None, None
    for _ in range(20):
        time.sleep(0.5)
        dev, pid = find_accessory()
        if dev:
            break
    if dev is None:
        print("[!] No re-enumero a accessory mode.")
        sys.exit(2)
    print(f"[ok] Accessory device {AOA_VID:04x}:{pid:04x}")

    # dar un instante a que la app arranque en modo USB
    time.sleep(1.0)

    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (usb.core.USBError, NotImplementedError):
        pass
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    print("[ok] Interfaz 0 reclamada. Escuchando ep 0x81 (IN) hasta 25s...\n")

    deadline = time.time() + 25
    total = 0
    while time.time() < deadline:
        try:
            data = dev.read(EP_IN, 16384, timeout=2000)
        except usb.core.USBError as e:
            if e.errno == 110:  # timeout
                print("    ... esperando (la app deberia mandar Identification)")
                continue
            print(f"[!] USBError en read: {e}")
            break
        b = bytes(data)
        total += len(b)
        print(f"[RX {len(b)} bytes] total={total}")
        # volcado completo de los 128 bytes
        for off in range(0, min(len(b), 128), 16):
            print(f"    {off:3d}: " + " ".join(f"{x:02x}" for x in b[off:off+16]))
        if len(b) >= 8:
            t = struct.unpack_from("<I", b, 0)[0]
            paylen = struct.unpack_from("<I", b, 4)[0]
            print(f"    -> type={t} ({TYPE_NAMES.get(t, '?')}), payloadLen={paylen}")
            if t == 0 and len(b) >= 96:
                vmaj = struct.unpack_from("<I", b, 8)[0]
                vmin = struct.unpack_from("<I", b, 12)[0]
                ctype = struct.unpack_from("<I", b, 16)[0]
                comp = struct.unpack_from("<I", b, 24)[0]
                subs = struct.unpack_from("<I", b, 28)[0]
                qual = struct.unpack_from("<I", b, 32)[0]
                rate = struct.unpack_from("<H", b, 44)[0]
                resmode = struct.unpack_from("<I", b, 48)[0]
                w = struct.unpack_from("<I", b, 52)[0]
                h = struct.unpack_from("<I", b, 88)[0]
                print(f"    -> Identification: v{vmaj}.{vmin}, clientType={ctype}, "
                      f"compression={comp}, subsampling={subs}, quality={qual}, "
                      f"frameRate={rate}, resMode={resmode}, resolucion={w}x{h}")
            print()
            break  # con el primer paquete entendido alcanza para esta sonda

    usb.util.release_interface(dev, 0)
    usb.util.dispose_resources(dev)
    print(f"[fin] Total recibido: {total} bytes.")
    if total == 0:
        print("[!] No llego nada. La app quiza no esta en modo USB display, o")
        print("    espera que el HOST hable primero. Probaremos mandar handshake nosotros.")


if __name__ == "__main__":
    main()
