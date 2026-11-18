"""MDR session: stop-and-wait delivery with 1-bit sequence numbers over a link."""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager

from . import protocol as p
from . import tables as t
from .frames import ACK_REQUIRED, TYPE_ACK, TYPE_DATA_MDR, TYPE_DATA_MDR_NO2, Frame, FrameParser, ack_for


class MdrError(Exception):
    pass


class MdrClient:
    def __init__(self, link, ack_timeout: float = 0.75, retries: int = 10):  # the app's values
        self._link = link
        self._parser = FrameParser()
        self._ack_timeout = ack_timeout
        self._retries = retries
        self._tx_seq = 0
        self._last_rx_seq: int | None = None
        self._acked = False
        self._inbox: deque[tuple[int, bytes]] = deque()

    def close(self) -> None:
        self._link.close()

    def _pump(self, deadline: float) -> None:
        """Read once from the link, acknowledging data frames as they arrive."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        data = self._link.recv(remaining)
        if not data:
            raise MdrError("link closed by headset")
        for frame in self._parser.feed(data):
            self._on_frame(frame)

    def _on_frame(self, frame: Frame) -> None:
        if frame.type == TYPE_ACK:
            if frame.seq != self._tx_seq:
                self._tx_seq = frame.seq
                self._acked = True
            return
        if frame.type in ACK_REQUIRED:
            self._link.send(ack_for(frame.seq).encode())
            if frame.seq == self._last_rx_seq:
                return  # retransmission: re-acked above, but already delivered
            self._last_rx_seq = frame.seq
        self._inbox.append((frame.type, frame.payload))

    def send(self, payload: bytes, table: int = 1) -> None:
        frame = Frame(TYPE_DATA_MDR if table == 1 else TYPE_DATA_MDR_NO2, self._tx_seq, payload).encode()
        self._acked = False
        for _ in range(self._retries + 1):
            self._link.send(frame)
            deadline = time.monotonic() + self._ack_timeout
            try:
                while not self._acked:
                    self._pump(deadline)
                return
            except TimeoutError:
                continue
        raise MdrError(f"no ACK for {p.command_name(payload, table)}")

    def wait_for(self, command: int, match: bytes = b"", table: int = 1, timeout: float = 5.0) -> bytes:
        """Return the next payload starting with command+match. Others stay queued."""
        wanted_type = TYPE_DATA_MDR if table == 1 else TYPE_DATA_MDR_NO2
        prefix = bytes([command]) + match
        deadline = time.monotonic() + timeout
        while True:
            for item in self._inbox:
                if item[0] == wanted_type and item[1].startswith(prefix):
                    self._inbox.remove(item)
                    return item[1]
            try:
                self._pump(deadline)
            except TimeoutError:
                raise MdrError(f"timed out waiting for command 0x{command:02X}") from None

    def drain(self, quiet: float) -> list[tuple[int, bytes]]:
        """Collect (table, payload) until nothing has arrived for `quiet` seconds."""
        try:
            while True:
                self._pump(time.monotonic() + quiet)
        except TimeoutError:
            pass
        items = [(1 if kind == TYPE_DATA_MDR else 2, payload) for kind, payload in self._inbox]
        self._inbox.clear()
        return items

    @contextmanager
    def serviced_in_background(self):
        """Keep acknowledging the headset's frames while another link is busy.

        The app keeps this link alive during an upload. The client must not be used
        by anything else inside the block. A lost link ends the service silently,
        because the headset reboots during an update.
        """
        stop = threading.Event()

        def service() -> None:
            while not stop.is_set():
                try:
                    self.drain(0.1)
                except (MdrError, OSError):
                    return
                stop.wait(0.05)

        thread = threading.Thread(target=service, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=3.0)

    def request(self, payload: bytes, response: str, table: int = 1, timeout: float = 5.0) -> bytes:
        # The inquired type (byte 1) is echoed in the response when there is one.
        match = payload[1:2]
        self.send(payload, table)
        commands = t.T1_COMMAND if table == 1 else t.T2_COMMAND
        return self.wait_for(commands[response], match, table, timeout)

    # Convenience wrappers over the commands the tools need.

    def handshake(self) -> int:
        """The headset answers nothing else until protocol info has been exchanged."""
        return p.parse_protocol_info(self.request(p.get_protocol_info(), "CONNECT_RET_PROTOCOL_INFO"))

    def device_string(self, kind: str) -> str:
        return p.parse_device_string(self.request(p.get_device_info(kind), "CONNECT_RET_DEVICE_INFO"))

    def battery(self) -> dict:
        return p.parse_battery(self.request(p.get_battery(), "COMMON_RET_BATTERY_LEVEL"))

    def set_fw_update_mode(self, enabled: bool) -> None:
        reply = self.request(p.set_fw_update_mode(enabled), "UPDT_NTFY_STATUS", timeout=10.0)
        if reply[2] != (0x00 if enabled else 0x01):
            raise MdrError(f"headset refused firmware update mode: {reply.hex()}")
