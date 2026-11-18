"""A simulated headset implementing the headset side of the protocols.

Behaviour follows what the app expects from a headset, so these tests prove the
client is self-consistent with that model. They cannot prove real hardware agrees.
"""
from __future__ import annotations

from collections import deque

from xm3.frames import Frame, FrameParser, ack_for
from xm3.gaia import (
    ACK_BIT, CMD_EVENT_NOTIFICATION, CMD_REGISTER_NOTIFICATION, CMD_VM_UPGRADE_CONNECT, CMD_VM_UPGRADE_CONTROL,
    CMD_VM_UPGRADE_DISCONNECT, EVENT_VMU_PACKET, VENDOR_QUALCOMM, GaiaPacket, GaiaParser, ResumePoint, VmuOp,
    vmu_decode, vmu_encode,
)


class SimLink:
    """In-memory link: bytes written go to a handler, replies queue for recv."""

    def __init__(self, handler):
        self._handler = handler
        self._out: deque[bytes] = deque()
        self.closed = False

    def reply(self, data: bytes) -> None:
        self._out.append(data)

    def send(self, data: bytes) -> None:
        self._handler(self, data)

    def recv(self, timeout: float) -> bytes:
        if not self._out:
            raise TimeoutError
        return self._out.popleft()

    def close(self) -> None:
        self.closed = True


class SimMdr:
    """MDR side: answers a dictionary of payload -> replies, acks everything."""

    def __init__(self, replies: dict[bytes, list[bytes]], drop_first_acks: int = 0):
        self.replies = replies
        self.received: list[bytes] = []
        self.drop = drop_first_acks
        self._parser = FrameParser()
        self._tx_seq = 0
        self._last_seq: int | None = None
        self.acks_from_client = 0
        self.link = SimLink(self._on_bytes)

    def _on_bytes(self, link: SimLink, data: bytes) -> None:
        for frame in self._parser.feed(data):
            if frame.type == 0x01:
                self.acks_from_client += 1
                self._tx_seq = frame.seq
                continue
            if self.drop:
                self.drop -= 1
                continue
            link.reply(ack_for(frame.seq).encode())
            if frame.seq == self._last_seq:
                continue
            self._last_seq = frame.seq
            self.received.append(frame.payload)
            for reply in self.replies.get(frame.payload, []):
                link.reply(Frame(frame.type, self._tx_seq, reply).encode())
                self._tx_seq = 1 - self._tx_seq


class SimUpgradeHeadset:
    """Headset side of the VM upgrade, with a reboot between transfer and install."""

    def __init__(self, image: bytes, chunk: int = 200, sync_errors: list[int] | None = None,
                 reject_validation: bool = False, commit: bool = True, skip_at_start: int = 0,
                 resume: ResumePoint = ResumePoint.DATA_TRANSFER):
        self.image = image
        self.chunk = chunk
        self.sync_errors = list(sync_errors or [])
        self.reject_validation = reject_validation
        self.commit = commit
        self.skip = skip_at_start
        self.resume = resume
        self.reboot_without_ack = True  # the app does not wait for this ACK, so neither may a client
        self.received = bytearray(image[:skip_at_start])
        self.connections = 0
        self.rebooted = False
        self.installed = False
        self.committed = False
        self.aborts = 0
        self.last_flag_seen = False
        self.sync_ids: list[bytes] = []
        self.link: SimLink | None = None
        self.parser = GaiaParser()

    def connect(self) -> SimLink:
        self.connections += 1
        self.parser = GaiaParser()
        self.link = SimLink(self._on_bytes)
        return self.link

    def _ack(self, command: int, status: int = 0) -> None:
        self.link.reply(GaiaPacket(VENDOR_QUALCOMM, command | ACK_BIT, bytes([status])).encode())

    def _vmu(self, op: int, data: bytes = b"") -> None:
        payload = bytes([EVENT_VMU_PACKET]) + vmu_encode(op, data)
        self.link.reply(GaiaPacket(VENDOR_QUALCOMM, CMD_EVENT_NOTIFICATION, payload).encode())

    def _request_bytes(self) -> None:
        remaining = len(self.image) - len(self.received)
        size = min(self.chunk, remaining)
        skip, self.skip = self.skip, 0
        self._vmu(VmuOp.DATA_BYTES_REQ, size.to_bytes(4, "big") + skip.to_bytes(4, "big"))

    def _on_bytes(self, link: SimLink, data: bytes) -> None:
        for packet in self.parser.feed(data):
            is_reboot_trigger = (packet.command == CMD_VM_UPGRADE_CONTROL
                                 and packet.payload[:1] == bytes([VmuOp.TRANSFER_COMPLETE_RES]))
            if not (is_reboot_trigger and self.reboot_without_ack):
                self._ack(packet.command)
            if packet.command == CMD_VM_UPGRADE_CONTROL:
                self._on_vmu(*vmu_decode(packet.payload))
            elif packet.command not in (CMD_VM_UPGRADE_CONNECT, CMD_REGISTER_NOTIFICATION, CMD_VM_UPGRADE_DISCONNECT):
                raise AssertionError(f"unexpected GAIA command {packet.command:04X}")

    def _on_vmu(self, op: int, data: bytes) -> None:
        if op == VmuOp.SYNC_REQ:
            self.sync_ids.append(data)
            if self.sync_errors:
                self._vmu(VmuOp.ERROR_WARN_IND, self.sync_errors.pop(0).to_bytes(2, "big"))
            else:
                self._vmu(VmuOp.SYNC_CFM, bytes([self.resume]) + bytes(5))
        elif op == VmuOp.ABORT_REQ:
            self.aborts += 1
            self._vmu(VmuOp.ABORT_CFM)
        elif op == VmuOp.START_REQ:
            self._vmu(VmuOp.START_CFM, b"\x00\x00\x00")
        elif op == VmuOp.START_DATA_REQ:
            self._request_bytes()
        elif op == VmuOp.DATA:
            self.last_flag_seen = data[0] == 1
            self.received += data[1:]
            if len(self.received) < len(self.image):
                self._request_bytes()
        elif op == VmuOp.IS_VALIDATION_DONE_REQ:
            if self.reject_validation:
                self._vmu(VmuOp.ERROR_WARN_IND, (0x0030).to_bytes(2, "big"))
            else:
                self._vmu(VmuOp.TRANSFER_COMPLETE_IND)
        elif op == VmuOp.TRANSFER_COMPLETE_RES:
            self.rebooted = True
            self.resume = ResumePoint.IN_PROGRESS
        elif op == VmuOp.IN_PROGRESS_RES:
            self._vmu(VmuOp.COMMIT_REQ if self.commit else VmuOp.COMPLETE_IND)
            self.installed = not self.commit
        elif op == VmuOp.COMMIT_CFM:
            self.committed = True
            self.installed = True
            self._vmu(VmuOp.COMPLETE_IND)
        else:
            raise AssertionError(f"unexpected VMU op {op:02X}")
