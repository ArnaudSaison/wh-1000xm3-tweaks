"""GAIA framing and the VM-upgrade packets tunnelled inside it.

    FF 01 flags len vendor:u16be command:u16be payload [xor]

Bit 0 of flags adds a trailing XOR of all previous bytes. Bit 15 of command marks
an acknowledgement, whose first payload byte is a status (0 = success).
See docs/protocol/03-firmware-update.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator

SOF = 0xFF
VERSION = 0x01
MAX_PAYLOAD = 254
FLAG_CHECKSUM = 0x01
ACK_BIT = 0x8000

VENDOR_QUALCOMM = 0x000A
VENDOR_SONY = 0x012D

CMD_VM_UPGRADE_CONNECT = 0x0640
CMD_VM_UPGRADE_DISCONNECT = 0x0641
CMD_VM_UPGRADE_CONTROL = 0x0642
CMD_REGISTER_NOTIFICATION = 0x4001
CMD_CANCEL_NOTIFICATION = 0x4002
CMD_EVENT_NOTIFICATION = 0x4003
EVENT_VMU_PACKET = 0x12


@dataclass(frozen=True)
class GaiaPacket:
    vendor: int
    command: int
    payload: bytes = b""
    flags: int = 0

    @property
    def is_ack(self) -> bool:
        return bool(self.command & ACK_BIT)

    @property
    def base_command(self) -> int:
        return self.command & ~ACK_BIT

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError(f"GAIA payload too long: {len(self.payload)}")
        head = bytes([SOF, VERSION, self.flags, len(self.payload)])
        raw = head + self.vendor.to_bytes(2, "big") + self.command.to_bytes(2, "big") + self.payload
        if self.flags & FLAG_CHECKSUM:
            check = 0
            for byte in raw:
                check ^= byte
            raw += bytes([check])
        return raw


class GaiaParser:
    """Incremental parser. Resynchronises on the 0xFF start-of-frame byte."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[GaiaPacket]:
        self._buffer += data
        while True:
            start = self._buffer.find(SOF)
            if start < 0:
                self._buffer.clear()
                return
            del self._buffer[:start]
            if len(self._buffer) < 4:
                return
            flags, length = self._buffer[2], self._buffer[3]
            total = 8 + length + (1 if flags & FLAG_CHECKSUM else 0)
            if len(self._buffer) < total:
                return
            raw = bytes(self._buffer[:total])
            del self._buffer[:total]
            yield GaiaPacket(int.from_bytes(raw[4:6], "big"), int.from_bytes(raw[6:8], "big"),
                             raw[8:8 + length], flags)


class VmuOp(IntEnum):
    """Upgrade opcodes observed in the app's state machine."""
    START_REQ = 0x01
    START_CFM = 0x02
    DATA_BYTES_REQ = 0x03
    DATA = 0x04
    ABORT_REQ = 0x07
    ABORT_CFM = 0x08
    TRANSFER_COMPLETE_IND = 0x0B
    TRANSFER_COMPLETE_RES = 0x0C
    IN_PROGRESS_RES = 0x0E
    COMMIT_REQ = 0x0F
    COMMIT_CFM = 0x10
    ERROR_WARN_IND = 0x11
    COMPLETE_IND = 0x12
    SYNC_REQ = 0x13
    SYNC_CFM = 0x14
    START_DATA_REQ = 0x15
    IS_VALIDATION_DONE_REQ = 0x16


class ResumePoint(IntEnum):
    DATA_TRANSFER = 0
    VALIDATION = 1
    TRANSFER_COMPLETE = 2
    IN_PROGRESS = 3
    COMMIT = 4


def vmu_encode(op: int, data: bytes = b"") -> bytes:
    return bytes([op]) + len(data).to_bytes(2, "big") + data


def vmu_decode(raw: bytes) -> tuple[int, bytes]:
    if len(raw) < 3:
        raise ValueError(f"VMU packet too short: {raw.hex()}")
    length = int.from_bytes(raw[1:3], "big")
    return raw[0], raw[3:3 + length]
