"""Firmware upgrade client: the transfer state machine, independent of Bluetooth.

The headset reboots once mid-way. `connect` is therefore a factory returning a
fresh GAIA link, called once before the transfer and again after the reboot.
"""
from __future__ import annotations

import hashlib
import time
from collections import deque
from typing import Callable

from .gaia import (
    CMD_EVENT_NOTIFICATION, CMD_REGISTER_NOTIFICATION, CMD_VM_UPGRADE_CONNECT, CMD_VM_UPGRADE_CONTROL,
    EVENT_VMU_PACKET, VENDOR_QUALCOMM, GaiaPacket, GaiaParser, ResumePoint, VmuOp, vmu_decode, vmu_encode,
)

MAX_CHUNK = 250
RECOVERABLE_ERRORS = {0x0023, 0x0081}  # the app answers these with abort-and-resync


class UpgradeError(Exception):
    pass


def sync_id(image: bytes) -> bytes:
    """The transfer identity sent in SYNC_REQ: the last 4 bytes of the image's MD5."""
    return hashlib.md5(image).digest()[-4:]


class GaiaSession:
    def __init__(self, link):
        self._link = link
        self._parser = GaiaParser()
        self._acks: deque[GaiaPacket] = deque()
        self._vmu: deque[tuple[int, bytes]] = deque()

    def close(self) -> None:
        self._link.close()

    def _pump(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpgradeError("timed out waiting for the headset")
        try:
            data = self._link.recv(remaining)
        except TimeoutError:
            raise UpgradeError("timed out waiting for the headset") from None
        if not data:
            raise UpgradeError("GAIA link closed by the headset")
        for packet in self._parser.feed(data):
            if packet.is_ack:
                self._acks.append(packet)
            elif (packet.command == CMD_EVENT_NOTIFICATION and packet.payload[:1] == bytes([EVENT_VMU_PACKET])):
                self._vmu.append(vmu_decode(packet.payload[1:]))

    def command(self, command: int, payload: bytes = b"", check: bool = True, timeout: float = 60.0) -> None:
        self._link.send(GaiaPacket(VENDOR_QUALCOMM, command, payload).encode())
        deadline = time.monotonic() + timeout
        while True:
            for ack in self._acks:
                if ack.base_command == command:
                    self._acks.remove(ack)
                    if check and ack.payload[:1] != b"\x00":
                        raise UpgradeError(f"command 0x{command:04X} rejected: status {ack.payload.hex()}")
                    return
            self._pump(deadline)

    def control(self, op: int, data: bytes = b"") -> None:
        self.command(CMD_VM_UPGRADE_CONTROL, vmu_encode(op, data))

    def control_no_wait(self, op: int, data: bytes = b"") -> None:
        """Send without waiting for the ACK, for a packet that makes the headset reboot."""
        self._link.send(GaiaPacket(VENDOR_QUALCOMM, CMD_VM_UPGRADE_CONTROL, vmu_encode(op, data)).encode())

    def expect(self, *ops: int, timeout: float = 60.0) -> tuple[int, bytes]:
        deadline = time.monotonic() + timeout
        while True:
            for item in self._vmu:
                if item[0] in ops:
                    self._vmu.remove(item)
                    return item
            self._pump(deadline)


class Upgrader:
    def __init__(self, image: bytes, connect: Callable[[], object], *, log: Callable[[str], None] = print,
                 progress: Callable[[int, int], None] = lambda done, total: None,
                 sleep: Callable[[float], None] = time.sleep,
                 reboot_timeout: float = 240.0, chunk_timeout: float = 60.0, install_timeout: float = 240.0):
        self.image = image
        self._connect = connect
        self._log = log
        self._progress = progress
        self._sleep = sleep
        self._reboot_timeout = reboot_timeout
        self._chunk_timeout = chunk_timeout
        self._install_timeout = install_timeout
        self._session: GaiaSession | None = None

    def run(self) -> None:
        try:
            resume = self._open(self._connect())
            if resume == ResumePoint.DATA_TRANSFER:
                self._transfer()
                self._close_session()
                resume = self._reconnect_after_reboot()
            if resume != ResumePoint.IN_PROGRESS:
                raise UpgradeError(f"unexpected resume point {ResumePoint(resume).name}")
            self._install()
        finally:
            self._close_session()

    def _close_session(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _open(self, link) -> ResumePoint:
        """Connect, sync and start. Returns where the headset wants to resume."""
        self._session = session = GaiaSession(link)
        # The app ignores this command's status, so a stale connection does not fail it.
        session.command(CMD_VM_UPGRADE_CONNECT, check=False)
        session.command(CMD_REGISTER_NOTIFICATION, bytes([EVENT_VMU_PACKET]))
        resume = self._sync(session, may_restart=True)
        session.control(VmuOp.START_REQ)
        session.expect(VmuOp.START_CFM)
        self._log(f"synchronised, resume point {resume.name}")
        return resume

    def _sync(self, session: GaiaSession, may_restart: bool) -> ResumePoint:
        session.control(VmuOp.SYNC_REQ, sync_id(self.image))
        op, data = session.expect(VmuOp.SYNC_CFM, VmuOp.ERROR_WARN_IND)
        if op == VmuOp.SYNC_CFM:
            point = data[0] if len(data) > 1 else 0
            try:
                return ResumePoint(point)
            except ValueError:
                raise UpgradeError(f"unknown resume point {point}") from None
        code = int.from_bytes(data[:2], "big")
        if code in RECOVERABLE_ERRORS and may_restart:
            self._log(f"headset reported error 0x{code:04X}: aborting and resyncing")
            session.control(VmuOp.ABORT_REQ)
            session.expect(VmuOp.ABORT_CFM)
            return self._sync(session, may_restart=False)
        raise UpgradeError(f"headset reported error 0x{code:04X} during sync")

    def _transfer(self) -> None:
        session = self._session
        total = len(self.image)
        position = 0
        session.control(VmuOp.START_DATA_REQ)
        while position < total:
            op, data = session.expect(VmuOp.DATA_BYTES_REQ, VmuOp.ERROR_WARN_IND, timeout=self._chunk_timeout)
            if op == VmuOp.ERROR_WARN_IND:
                raise UpgradeError(f"headset reported error 0x{int.from_bytes(data[:2], 'big'):04X} in transfer")
            size = int.from_bytes(data[:4], "big")
            position += int.from_bytes(data[4:8], "big")  # offset is relative: bytes to skip forward
            if not 0 <= size <= MAX_CHUNK:
                size = MAX_CHUNK
            chunk = self.image[position:position + size]
            position += len(chunk)
            session.control(VmuOp.DATA, bytes([1 if position >= total else 0]) + chunk)
            self._progress(min(position, total), total)
        self._log("all data sent, waiting for the headset to validate the image")
        session.control(VmuOp.IS_VALIDATION_DONE_REQ)
        op, data = session.expect(VmuOp.TRANSFER_COMPLETE_IND, VmuOp.ERROR_WARN_IND, timeout=self._install_timeout)
        if op == VmuOp.ERROR_WARN_IND:
            raise UpgradeError(f"headset rejected the image: error 0x{int.from_bytes(data[:2], 'big'):04X}")
        self._log("image validated, telling the headset to reboot into it")
        # Like the app, do not wait for an ACK here: the headset may reboot first.
        session.control_no_wait(VmuOp.TRANSFER_COMPLETE_RES, b"\x00")
        self._sleep(1.0)  # let the packet leave before the socket closes

    def _reconnect_after_reboot(self) -> ResumePoint:
        self._log("headset is rebooting, waiting for it to come back")
        deadline = time.monotonic() + self._reboot_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self._sleep(5.0)
            try:
                link = self._connect()
            except OSError as err:
                last_error = err
                continue
            self._sleep(0.3)  # the app waits this long before its first GAIA command
            return self._open(link)
        raise UpgradeError(f"headset did not come back after reboot: {last_error}")

    def _install(self) -> None:
        session = self._session
        session.control(VmuOp.IN_PROGRESS_RES, b"\x00")
        op, data = session.expect(VmuOp.COMPLETE_IND, VmuOp.COMMIT_REQ, VmuOp.ERROR_WARN_IND,
                                  timeout=self._install_timeout)
        if op == VmuOp.ERROR_WARN_IND:
            raise UpgradeError(f"headset reported error 0x{int.from_bytes(data[:2], 'big'):04X} during install")
        if op == VmuOp.COMMIT_REQ:
            session.control(VmuOp.COMMIT_CFM, b"\x00")
            session.expect(VmuOp.COMPLETE_IND, timeout=self._install_timeout)
        self._log("update complete")  # like the app, just close the link; run() does that
