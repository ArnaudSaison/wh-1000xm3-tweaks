#!/usr/bin/env python3
"""Decode MDR and GAIA traffic from an Android Bluetooth HCI snoop log.

    uv run tools/btsnoop_mdr.py btsnoop_hci.log

Follows each RFCOMM channel in each direction and prints the frames found. This
is the way to check docs/protocol/ against what a real headset does.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xm3 import protocol as p  # noqa: E402
from xm3.frames import END, START, FrameParser  # noqa: E402
from xm3.gaia import GaiaParser, vmu_decode  # noqa: E402

BTSNOOP_MAGIC = b"btsnoop\0"
HCI_ACL = 0x02
RFCOMM_UIH = {0xEF, 0xFF}


def records(data: bytes):
    """Yield (received, hci_packet) from a btsnoop v1 file with H4 datalink."""
    if data[:8] != BTSNOOP_MAGIC:
        raise SystemExit("not a btsnoop file")
    pos = 16
    while pos + 24 <= len(data):
        _, included, flags, _, _ = struct.unpack(">IIIIQ", data[pos:pos + 24])
        yield bool(flags & 1), data[pos + 24:pos + 24 + included]
        pos += 24 + included


def rfcomm_payloads(stream):
    """Yield (key, data), reassembling fragmented ACL into L2CAP frames first."""
    pending: dict[tuple, bytearray] = {}
    for received, packet in stream:
        if not packet or packet[0] != HCI_ACL or len(packet) < 5:
            continue
        word = struct.unpack("<H", packet[1:3])[0]
        handle, boundary = word & 0x0FFF, (word >> 12) & 0x3
        fragment = packet[5:]
        key = (handle, received)
        if boundary == 1:
            if key in pending:
                pending[key] += fragment
        else:
            pending[key] = bytearray(fragment)
        buffer = pending.get(key)
        if buffer is None or len(buffer) < 4:
            continue
        length, cid = struct.unpack("<HH", buffer[:4])
        if len(buffer) < 4 + length:
            continue
        l2cap = bytes(buffer[4:4 + length])
        del pending[key]
        if cid < 0x40 or len(l2cap) < 4 or l2cap[1] not in RFCOMM_UIH:
            continue
        dlci = l2cap[0] >> 2
        if dlci == 0:
            continue  # multiplexer control channel
        size = l2cap[2] >> 1
        pos = 3
        if not l2cap[2] & 1:
            size |= l2cap[3] << 7
            pos = 4
        if l2cap[1] == 0xFF:
            pos += 1  # credit byte
        data = l2cap[pos:pos + size]
        if data:
            yield (handle, cid, dlci, received), data


class Channel:
    """Decides per channel whether the stream is MDR or GAIA, from the first start byte."""

    def __init__(self) -> None:
        self.mdr = FrameParser()
        self.gaia = GaiaParser()
        self.kind: str | None = None

    def feed(self, data: bytes, arrow: str, tag: str) -> None:
        if self.kind is None:
            self.kind = {START: "mdr", 0xFF: "gaia"}.get(data[0])
        if self.kind == "mdr":
            for frame in self.mdr.feed(data):
                if frame.type == 0x01:
                    print(f"{tag} {arrow} MDR ack seq={frame.seq}")
                else:
                    table = 2 if frame.type in (0x0E, 0x1E) else 1
                    name = p.command_name(frame.payload, table)
                    print(f"{tag} {arrow} MDR type={frame.type:02X} seq={frame.seq} {name:32} {frame.payload.hex(' ')}")
        elif self.kind == "gaia":
            for packet in self.gaia.feed(data):
                extra = ""
                if packet.command == 0x0642 or (packet.command == 0x4003 and packet.payload[:1] == b"\x12"):
                    raw = packet.payload if packet.command == 0x0642 else packet.payload[1:]
                    op, body = vmu_decode(raw)
                    extra = f" VMU op=0x{op:02X} len={len(body)}"
                    packet_text = body[:16].hex(" ")
                else:
                    packet_text = packet.payload.hex(" ")
                print(f"{tag} {arrow} GAIA vendor={packet.vendor:04X} cmd={packet.command:04X}{extra} {packet_text}")


def main() -> None:
    channels: dict[tuple, Channel] = {}
    for key, data in rfcomm_payloads(records(Path(sys.argv[1]).read_bytes())):
        handle, cid, dlci, received = key
        tag = f"[h{handle:03X} dlci{dlci:02}]"
        channels.setdefault(key, Channel()).feed(data, "<-" if received else "->", tag)
    if not channels:
        print("no RFCOMM data found: was the capture started before connecting?")


if __name__ == "__main__":
    main()
