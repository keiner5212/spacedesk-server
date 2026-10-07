#!/usr/bin/env python3
"""
Handshake Android Open Accessory (AOA) con la tablet spacedesk.

La PC actua como HOST USB. Pone la tablet en "accessory mode" mandando los
control transfers estandar de AOA con los strings que la app spacedesk declara
en su accessory_filter (manufacturer=datronicsoft, model=spacedesk). Si todo va
bien, la tablet se RE-ENUMERA con VID 0x18d1 (Google) PID 0x2d00/0x2d01 y expone
endpoints bulk por los que despues se habla el mismo protocolo binario de
spacedesk (header 128B) que ya usamos sobre TCP.

Este script SOLO hace el handshake y reporta el resultado. No transmite video.
"""
import sys
import time
import usb.core
import usb.util

# Tablet en modo normal (antes del switch a accessory)
NORMAL_VID = 0x339b
NORMAL_PID = 0x107d

# PIDs de Google cuando un dispositivo entra en accessory mode (AOA)
AOA_VID = 0x18d1
AOA_PIDS = {
    0x2d00: "accessory",
    0x2d01: "accessory+adb",
    0x2d02: "audio",
    0x2d03: "audio+adb",
    0x2d04: "accessory+audio",
    0x2d05: "accessory+audio+adb",
}

# Requests AOA
ACCESSORY_GET_PROTOCOL = 51
ACCESSORY_SEND_STRING = 52
ACCESSORY_START = 53

# Indices de string AOA
STR_MANUFACTURER = 0
STR_MODEL = 1
STR_DESCRIPTION = 2
STR_VERSION = 3
STR_URI = 4
STR_SERIAL = 5

# Lo que spacedesk exige que coincida (sacado de res/xml/accessory_filter.xml)
STRINGS = {
    STR_MANUFACTURER: "datronicsoft",
    STR_MODEL: "spacedesk",
    # la app filtra por getDescription().endsWith(" (spacedesk)") -> ver SAActivityDisplayUsb.i1()
    STR_DESCRIPTION: "Linux PC (spacedesk)",
    STR_VERSION: "1.0",
    STR_URI: "https://www.spacedesk.net",
    STR_SERIAL: "0000000012345678",
}


def find_accessory():
    for pid, name in AOA_PIDS.items():
        dev = usb.core.find(idVendor=AOA_VID, idProduct=pid)
        if dev is not None:
            return dev, pid, name
    return None, None, None


def main():
    # 0) Si ya esta en accessory mode, no rehacer el handshake
    dev, pid, name = find_accessory()
    if dev is not None:
        print(f"[i] La tablet YA esta en accessory mode: {AOA_VID:04x}:{pid:04x} ({name})")
        dump_endpoints(dev)
        return

    # 1) Encontrar la tablet en modo normal
    dev = usb.core.find(idVendor=NORMAL_VID, idProduct=NORMAL_PID)
    if dev is None:
        print(f"[!] No encuentro la tablet {NORMAL_VID:04x}:{NORMAL_PID:04x}.")
        print("    Conectala por USB y desbloquea la pantalla.")
        sys.exit(1)
    print(f"[ok] Tablet encontrada: {NORMAL_VID:04x}:{NORMAL_PID:04x}")

    # 2) Preguntar version de protocolo AOA (control IN, request 51)
    try:
        ret = dev.ctrl_transfer(
            bmRequestType=0xC0,  # IN | Vendor | Device
            bRequest=ACCESSORY_GET_PROTOCOL,
            wValue=0, wIndex=0, data_or_wLength=2, timeout=2000,
        )
    except usb.core.USBError as e:
        print(f"[!] Fallo GET_PROTOCOL: {e}")
        print("    (Si es permiso: necesitamos una regla udev o correr con sudo.)")
        sys.exit(2)

    proto = ret[0] | (ret[1] << 8)
    print(f"[ok] AOA soportado. Version de protocolo = {proto}")
    if proto == 0:
        print("[!] Version 0 = el dispositivo dice NO soportar AOA. Abortando.")
        sys.exit(3)

    # 3) Mandar los strings de identificacion (control OUT, request 52)
    for idx, val in STRINGS.items():
        payload = val.encode("utf-8") + b"\x00"
        dev.ctrl_transfer(
            bmRequestType=0x40,  # OUT | Vendor | Device
            bRequest=ACCESSORY_SEND_STRING,
            wValue=0, wIndex=idx, data_or_wLength=payload, timeout=2000,
        )
        print(f"    [str {idx}] {val!r} enviado")

    # 4) Pedir el cambio a accessory mode (control OUT, request 53, sin datos)
    dev.ctrl_transfer(
        bmRequestType=0x40,
        bRequest=ACCESSORY_START,
        wValue=0, wIndex=0, data_or_wLength=None, timeout=2000,
    )
    print("[ok] ACCESSORY_START enviado. La tablet deberia re-enumerarse...")
    print("     (En la tablet puede aparecer un dialogo 'Abrir spacedesk?' -> aceptar)")

    # liberar el handle viejo antes de que el device desaparezca
    usb.util.dispose_resources(dev)

    # 5) Esperar la re-enumeracion con el nuevo VID/PID
    for i in range(20):
        time.sleep(0.5)
        dev, pid, name = find_accessory()
        if dev is not None:
            print(f"\n[OK] Re-enumerado en accessory mode: "
                  f"{AOA_VID:04x}:{pid:04x} ({name})")
            dump_endpoints(dev)
            return
    print("\n[!] No aparecio en accessory mode tras 10s.")
    print("    Posibles causas: el dialogo en la tablet no se acepto, la app no")
    print("    esta instalada, o el manufacturer/model no coinciden.")


def dump_endpoints(dev):
    try:
        cfg = dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
        cfg = dev.get_active_configuration()
    print("    Interfaces/endpoints disponibles:")
    for intf in cfg:
        print(f"      intf {intf.bInterfaceNumber} class={intf.bInterfaceClass}")
        for ep in intf:
            d = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
            t = usb.util.endpoint_type(ep.bmAttributes)
            print(f"        ep 0x{ep.bEndpointAddress:02x} {d} type={t} maxpkt={ep.wMaxPacketSize}")


if __name__ == "__main__":
    main()
