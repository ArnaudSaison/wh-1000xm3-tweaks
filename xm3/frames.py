"""MDR frame codec: the byte-level layer of the headset's control channel.

Wire format (see docs/protocol/01-mdr-transport.md):

    3E | escaped( type | seq | len:u32be | payload | checksum ) | 3C

checksum is the low byte of the sum of type..payload, computed before escaping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

START = 0x3E
END = 0x3C
ESCAPE = 0x3D
ESCAPE_BIT = 0x10  # 3C/3D/3E travel as 3D followed by 2C/2D/2E

TYPE_DATA_MDR = 0x0C
TYPE_DATA_MDR_NO2 = 0x0E
TYPE_ACK = 0x01

# Frame types the receiver must acknowledge. Types absent here are not acked.
ACK_REQUIRED = {0x00, 0x02, 0x07, 0x09, 0x0A, 0x0C, 0x0D, 0x0E, 0x27, 0x2C, 0x2D}


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    payload: bytes = b""

    def encode(self) -> bytes:
        body = bytes([self.type, self.seq]) + len(self.payload).to_bytes(4, "big") + self.payload
        body += bytes([sum(body) & 0xFF])
        return bytes([START]) + _escape(body) + bytes([END])


def ack_for(seq: int) -> Frame:
    """The ACK answering a received frame carries the opposite sequence number."""
    return Frame(TYPE_ACK, 1 - seq)


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        if byte in (START, END, ESCAPE):
            out += bytes([ESCAPE, byte & ~ESCAPE_BIT])
        else:
            out.append(byte)
    return bytes(out)


def _unescape(data: bytes) -> bytes:
    out = bytearray()
    it = iter(data)
    for byte in it:
        if byte == ESCAPE:
            try:
                out.append(next(it) | ESCAPE_BIT)
            except StopIteration:
                raise FrameError("escape byte at end of frame") from None
        else:
            out.append(byte)
    return bytes(out)


def decode(raw: bytes) -> Frame:
    """Decode the bytes between the START and END markers."""
    body = _unescape(raw)
    if len(body) < 7:
        raise FrameError(f"frame too short: {body.hex()}")
    if sum(body[:-1]) & 0xFF != body[-1]:
        raise FrameError(f"bad checksum: {body.hex()}")
    length = int.from_bytes(body[2:6], "big")
    if length != len(body) - 7:
        raise FrameError(f"length field {length} != actual {len(body) - 7}")
    return Frame(body[0], body[1], body[6:-1])


class FrameParser:
    """Incremental parser. Bytes before a START marker are discarded."""

    def __init__(self) -> None:
        self._buffer: bytearray | None = None
        self.errors: list[FrameError] = []

    def feed(self, data: bytes) -> Iterator[Frame]:
        for byte in data:
            if self._buffer is None:
                if byte == START:
                    self._buffer = bytearray()
            elif byte == END:
                raw, self._buffer = bytes(self._buffer), None
                try:
                    yield decode(raw)
                except FrameError as err:
                    self.errors.append(err)
            else:
                self._buffer.append(byte)
