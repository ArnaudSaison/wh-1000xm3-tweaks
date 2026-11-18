"""RFCOMM links to the headset, opened by SDP service UUID.

A "link" is anything with send(bytes), recv(timeout) -> bytes and close().
recv raises TimeoutError when nothing arrives and returns b"" once closed.
"""
from __future__ import annotations

import ctypes
import re
import shutil
import socket
import subprocess
import sys
import uuid

MDR_V1_UUID = "96CC203E-5068-46AD-B32D-E316F5E069BA"
MDR_V2_UUID = "956C7B26-D49A-4BA8-B03F-B17D393CB6E2"
GAIA_UUID = "7B265B0E-2232-4D45-BEF4-BB8AE62F813D"


class SocketLink:
    def __init__(self, sock: socket.socket):
        self._sock = sock

    def send(self, data: bytes) -> None:
        self._sock.settimeout(30)
        self._sock.sendall(data)

    def recv(self, timeout: float) -> bytes:
        self._sock.settimeout(timeout)
        try:
            return self._sock.recv(4096)
        except socket.timeout:
            raise TimeoutError from None

    def close(self) -> None:
        self._sock.close()


class _SockaddrBth(ctypes.Structure):
    """Windows SOCKADDR_BTH. Port 0 with a service class asks the stack to run SDP."""
    _pack_ = 1
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("address", ctypes.c_ulonglong),
        ("service", ctypes.c_byte * 16),
        ("port", ctypes.c_ulong),
    ]


def _connect_windows(address: str, service: str, channel: int | None) -> socket.socket:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    target = _SockaddrBth()
    target.family = socket.AF_BLUETOOTH
    target.address = int(address.replace(":", ""), 16)
    target.port = channel or 0
    if not channel:
        ctypes.memmove(target.service, uuid.UUID(service).bytes_le, 16)
    ws2 = ctypes.WinDLL("ws2_32", use_last_error=True)
    if ws2.connect(ctypes.c_size_t(sock.fileno()), ctypes.byref(target), ctypes.sizeof(target)) != 0:
        code = ctypes.get_last_error()
        sock.close()
        raise OSError(code, f"RFCOMM connect to {address} service {service} failed (WSA error {code})")
    return sock


def _sdp_channel_linux(address: str, service: str) -> int:
    if not shutil.which("sdptool"):
        raise OSError("sdptool not found: install bluez-deprecated tools or pass --channel")
    out = subprocess.run(["sdptool", "search", "--bdaddr", address, service],
                         capture_output=True, text=True, timeout=30).stdout
    match = re.search(r"Channel:\s*(\d+)", out)
    if not match:
        raise OSError(f"service {service} not advertised by {address}")
    return int(match[1])


def connect(address: str, service: str, channel: int | None = None) -> SocketLink:
    if sys.platform == "win32":
        return SocketLink(_connect_windows(address, service, channel))
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    sock.connect((address, channel or _sdp_channel_linux(address, service)))
    return SocketLink(sock)
