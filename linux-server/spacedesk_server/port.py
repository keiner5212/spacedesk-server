"""
Libera el puerto del servidor antes de arrancar.

Sin esto, una instancia vieja que quedo viva (Ctrl+C a medias, un `./start.sh`
anterior en otra terminal) hace que el bind nuevo COMPARTA el puerto en vez de
fallar: el proceso nuevo levanta "bien", el cliente se conecta al viejo, y el
sintoma es "a veces funciona y a veces no" sin ningun error en el log.

Se resuelve en dos pasos:
  1. Matar lo que tenga el puerto tomado (TCP y UDP, IPv4 e IPv6).
  2. Quitar `reuse_port` de los binds, para que si queda algo en pie el
     arranque falle con un error claro en vez de repartirse el trafico.
"""

import errno
import logging
import os
import signal
import time
from pathlib import Path

log = logging.getLogger("spacedesk.port")

PROC_NET = ("tcp", "tcp6", "udp", "udp6")


def _socket_inodes(port: int) -> set[str]:
    """Inodes de los sockets que tienen el puerto local dado, leyendo
    /proc/net/{tcp,tcp6,udp,udp6}. La columna del inode es la que permite
    despues encontrar el proceso dueño."""
    inodes: set[str] = set()
    for proto in PROC_NET:
        try:
            lines = Path(f"/proc/net/{proto}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local_port == port and fields[9] != "0":
                inodes.add(fields[9])
    return inodes


def _pids_owning(inodes: set[str]) -> set[int]:
    """PIDs que tienen abierto alguno de esos sockets."""
    if not inodes:
        return set()
    targets = {f"socket:[{inode}]" for inode in inodes}
    pids: set[int] = set()
    own_pid = os.getpid()
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        if pid == own_pid:
            continue
        fd_dir = proc_dir / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if os.readlink(fd) in targets:
                        pids.add(pid)
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return pids


def free_port(port: int, grace: float = 2.0) -> None:
    """Termina los procesos que tengan el puerto tomado y espera a que lo
    liberen. Es inocuo si el puerto ya esta libre."""
    pids = _pids_owning(_socket_inodes(port))
    if not pids:
        return

    for pid in sorted(pids):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            cmdline = "?"
        log.warning("Puerto %d tomado por el proceso %d (%s), terminandolo", port, pid, cmdline.strip())

    for pid in sorted(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            log.error("Sin permiso para terminar el proceso %d que tiene el puerto %d", pid, port)
            raise

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _socket_inodes(port):
            log.info("Puerto %d liberado", port)
            return
        time.sleep(0.1)

    for pid in sorted(_pids_owning(_socket_inodes(port))):
        log.warning("El proceso %d no termino con SIGTERM, usando SIGKILL", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    time.sleep(0.2)
    if _socket_inodes(port):
        raise OSError(errno.EADDRINUSE, f"El puerto {port} sigue ocupado por otro proceso")


def is_free(port: int) -> bool:
    return not _socket_inodes(port)