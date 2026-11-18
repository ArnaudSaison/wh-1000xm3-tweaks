"""Owns the headset connection for the web UI: status, live updates, and upload jobs.

One lock guards the MDR link, because the protocol allows one outstanding frame.
Nothing here writes to the headset except the upload jobs, which the user starts.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from xm3 import protocol as p
from xm3 import tables as t
from xm3.client import MdrClient, MdrError
from xm3.gaia import ResumePoint
from xm3.package import PackageError, VoicePackage
from xm3.transport import GAIA_UUID, MDR_V1_UUID, connect as rfcomm_connect
from xm3.upgrade import Upgrader, UpgradeError

MODEL = "WH-1000XM3"
FIRMWARE_VERSION = "4.5.2"  # the only version the firmware patch has been analysed for
LIVE_BATTERY_SECONDS = 30
BUTTON_LABELS = {
    "ASSIGNABLE_KEY_ELEM_NCASM": "Noise control",
    "ASSIGNABLE_KEY_ELEM_GOOGLE_ASSISTANT": "Google Assistant",
    "ASSIGNABLE_KEY_ELEM_AMAZON_ALEXA": "Alexa",
}
RECONNECT_ATTEMPTS = 6
RECONNECT_DELAY_SECONDS = 5
_C1 = t.T1_COMMAND


@dataclass
class FirmwareImage:
    """A full firmware file. Deliberately not a VoicePackage, so the voice path refuses it."""
    raw: bytes
    sha1: str


def discover_paired() -> list[dict]:
    """Find paired WH-1000XM3 headsets (Windows only; elsewhere enter the address)."""
    if sys.platform != "win32":
        return []
    script = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
              "Where-Object { $_.FriendlyName -eq 'WH-1000XM3' -and $_.InstanceId -match 'DEV_' } | "
              "ForEach-Object { $_.InstanceId }")
    try:
        output = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = {m.group(1) for m in re.finditer(r"DEV_([0-9A-F]{12})", output)}
    return [{"name": MODEL, "address": ":".join(h[i:i + 2] for i in range(0, 12, 2))} for h in sorted(found)]


def nc_summary(nc: dict) -> str:
    """Labels for nc_value come from a published XM3 capture, not from the app."""
    if nc["effect"] == "OFF":
        return "Off"
    if nc["nc_value"] == 2:
        return "Noise cancelling"
    if nc["nc_value"] == 1:
        return "Wind noise reduction"
    return f"Ambient sound, level {nc['asm_value']}" + (", voice focus" if nc["asm_id"] == "VOICE" else "")


def _text_reply(reply: bytes) -> str:
    return reply[3:3 + reply[2]].decode("utf-8", "replace")


class DeviceService:
    def __init__(self, mdr_connect: Callable = None, gaia_connect: Callable = None):
        self._mdr_connect = mdr_connect or (lambda address: rfcomm_connect(address, MDR_V1_UUID))
        self._gaia_connect = gaia_connect or (lambda address: rfcomm_connect(address, GAIA_UUID))
        self._lock = threading.RLock()
        self.client: MdrClient | None = None
        self.address: str | None = None
        self.status: dict = {}
        self.log: deque = deque(maxlen=500)
        self.job = {"active": False, "kind": None, "phase": "idle", "percent": 0, "result": None, "simulated": False,
                    "firmware": False}
        self._last_battery = 0.0
        threading.Thread(target=self._live_loop, daemon=True).start()

    def note(self, kind: str, message: str) -> None:
        self.log.append({"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": message})

    @property
    def connected(self) -> bool:
        return self.client is not None

    # --- connection and status -------------------------------------------------

    def connect(self, address: str) -> dict:
        if self.job["active"]:
            raise MdrError("an upload is running")
        with self._lock:
            self.disconnect()
            self.note("info", f"connecting to {address}...")
            client = MdrClient(self._mdr_connect(address))
            try:
                client.handshake()
                self.client, self.address = client, address
                self.refresh()
            except (MdrError, OSError):
                client.close()
                self.client = None
                raise
            self.note("ok", f"connected: {self.status['model']}, firmware {self.status['firmware']}")
            return self.status

    def disconnect(self) -> None:
        with self._lock:
            if self.client is not None:
                try:
                    self.client.close()
                except OSError:
                    pass
            self.client = None
            self.status = {}

    def refresh(self) -> dict:
        """Read everything the dashboard shows, using GET commands only."""
        with self._lock:
            c = self._need_client()
            s: dict = {
                "model": c.device_string("MODEL_NAME"),
                "firmware": c.device_string("FW_VERSION"),
                "battery": c.battery(),
                "codec": p.name_of(t.AUDIO_CODEC, c.request(p.get_codec(), "COMMON_RET_AUDIO_CODEC")[2]),
                "nc": nc_summary(p.parse_ncasm(c.request(p.get_ncasm(), "NCASM_RET_PARAM"))),
                "eq": p.parse_eq(c.request(p.get_eq(), "EQEBB_RET_PARAM"))["preset"],
            }
            for kind, key in (("SERVICE_ID", "service"), ("NATION_CODE", "region")):
                s[key] = _text_reply(c.request(p.get_update_param(kind), "UPDT_RET_PARAM"))
            s["battery_threshold"] = c.request(p.get_update_param("BATTERY_POWER_THRESHOLD"), "UPDT_RET_PARAM")[2]
            for which, key in ((p.GS1, "custom_button"), (p.GS2, "touch_panel")):
                info = p.parse_general_capability(c.request(p.get_general_capability(which),
                                                            "GENERAL_SETTING_RET_CAPABILITY"))
                value = p.parse_general(c.request(p.get_general(which), "GENERAL_SETTING_RET_PARAM"))["value"]
                if info["type"] == "list" and value < len(info["options"]):
                    s[key] = BUTTON_LABELS.get(info["options"][value], info["options"][value])
                else:
                    s[key] = "On" if value else "Off"
            s["voice_on"] = c.request(p.vg_get_onoff(), "VOICE_GUIDANCE_RET_PARAM", table=2)[3] == 1
            language = c.request(p.vg_get_language(), "VOICE_GUIDANCE_RET_PARAM", table=2)[3]
            s["language_code"] = language
            s["language"] = p.name_of(t.MDR_LANGUAGE, language).replace("_", " ").title()
            self.status = s
            self._last_battery = time.monotonic()
            return s

    def survey(self) -> list[dict]:
        """The read-only query list from the protocol module, with raw replies."""
        with self._lock:
            c = self._need_client()
            rows = []
            for table, text in p.SURVEY:
                payload = bytes.fromhex(text)
                c.send(payload, table)
                replies = c.drain(0.4)
                rows.append({"name": p.command_name(payload, table), "sent": payload.hex(" "),
                             "replies": [{"name": p.command_name(r, tb), "data": r.hex(" ")} for tb, r in replies]})
            return rows

    def _need_client(self) -> MdrClient:
        if self.client is None:
            raise MdrError("headset not connected")
        return self.client

    def _live_loop(self) -> None:
        while True:
            time.sleep(1.0)
            if self.client is None or self.job["active"]:
                continue
            if not self._lock.acquire(timeout=0.2):
                continue
            try:
                self._live_step()
            except (MdrError, OSError) as err:
                self.note("warn", f"connection lost: {err}")
                self.disconnect()
            finally:
                self._lock.release()

    def _live_step(self) -> None:
        client = self.client
        if client is None:
            return
        for table, payload in client.drain(0.05):
            if table == 1 and payload[:1] == bytes([_C1["COMMON_NTFY_BATTERY_LEVEL"]]):
                self.status["battery"] = p.parse_battery(payload)
            elif table == 1 and payload[:2] == bytes([_C1["NCASM_NTFY_PARAM"], 0x02]):
                self.status["nc"] = nc_summary(p.parse_ncasm(payload))
                self.note("info", f"headset changed mode: {self.status['nc']}")
        if time.monotonic() - self._last_battery > LIVE_BATTERY_SECONDS:
            self._last_battery = time.monotonic()
            self.status["battery"] = client.battery()

    # --- uploads ---------------------------------------------------------------

    def preflight(self, package: VoicePackage | None) -> list[dict]:
        """The checks shown before an upload. Every one must pass to enable the button."""
        s = self.status
        checks = [{"id": "connected", "label": "Headset connected", "ok": self.connected, "detail": self.address or ""}]
        if self.connected:
            checks.append({"id": "model", "label": f"Model is {MODEL}", "ok": s.get("model") == MODEL,
                           "detail": s.get("model", "")})
            level, threshold = s["battery"]["level"], s["battery_threshold"]
            checks.append({"id": "battery", "label": f"Battery above {threshold}%", "ok": level > threshold,
                           "detail": f"{level}%"})
        checks.append({"id": "package", "label": "Package chosen and valid", "ok": package is not None,
                       "detail": package.sha1[:12] if package else ""})
        if self.connected and package is not None:
            checks.append({"id": "language", "label": "Package language matches headset",
                           "ok": package.language == s.get("language_code"),
                           "detail": p.name_of(t.MDR_LANGUAGE, package.language or 0)})
        return checks

    def preflight_firmware(self, image: FirmwareImage | None, file_error: str | None) -> list[dict]:
        """The checks shown before a firmware upload. Every one must pass to enable the button."""
        s = self.status
        checks = [{"id": "connected", "label": "Headset connected", "ok": self.connected, "detail": self.address or ""}]
        if self.connected:
            checks.append({"id": "model", "label": f"Model is {MODEL}", "ok": s.get("model") == MODEL,
                           "detail": s.get("model", "")})
            checks.append({"id": "version", "label": f"Headset runs firmware {FIRMWARE_VERSION}",
                           "ok": s.get("firmware") == FIRMWARE_VERSION, "detail": s.get("firmware", "")})
            level, threshold = s["battery"]["level"], s["battery_threshold"]
            checks.append({"id": "battery", "label": f"Battery above {threshold}%", "ok": level > threshold,
                           "detail": f"{level}%"})
        checks.append({"id": "image", "label": "Firmware file built", "ok": image is not None,
                       "detail": image.sha1[:12] if image else ""})
        if image is not None:
            checks.append({"id": "file", "label": "Signatures, checksums and region pass offline",
                           "ok": file_error is None, "detail": file_error or ""})
        return checks

    def test_gaia(self) -> None:
        """Open and immediately close the update channel. Sends no bytes."""
        with self._lock:
            self._need_client()
            self.note("info", "opening the update channel (no data is sent)...")
            self._gaia_connect(self.address).close()
            self.note("ok", "update channel opened and closed")

    def start_upload(self, package: VoicePackage, label: str) -> None:
        if self.job["active"]:
            raise MdrError("an upload is already running")
        failed = [c["label"] for c in self.preflight(package) if not c["ok"]]
        if failed:
            raise MdrError("checks not passed: " + "; ".join(failed))
        self._start_job(self._run_real_upload, package, label, simulated=False)

    def start_simulation(self, package: VoicePackage, label: str) -> None:
        if self.job["active"]:
            raise MdrError("an upload is already running")
        self._start_job(self._run_simulation, package, label, simulated=True)

    def start_firmware_upload(self, image: FirmwareImage, label: str, file_error: str | None) -> None:
        if self.job["active"]:
            raise MdrError("an upload is already running")
        failed = [c["label"] for c in self.preflight_firmware(image, file_error) if not c["ok"]]
        if failed:
            raise MdrError("checks not passed: " + "; ".join(failed))
        self._start_job(self._run_real_firmware, image, label, simulated=False, firmware=True)

    def start_firmware_simulation(self, image: FirmwareImage, label: str) -> None:
        if self.job["active"]:
            raise MdrError("an upload is already running")
        self._start_job(self._run_simulation, image, label, simulated=True, firmware=True)

    def _start_job(self, target, package, label: str, simulated: bool, firmware: bool = False) -> None:
        self.job = {"active": True, "kind": label, "phase": "starting", "percent": 0, "result": None,
                    "simulated": simulated, "firmware": firmware}
        threading.Thread(target=target, args=(package, label), daemon=True).start()

    def _phase(self, message: str) -> None:
        self.job["phase"] = message
        self.note("info", ("[simulated] " if self.job["simulated"] else "") + message)

    def _progress(self, done: int, total: int) -> None:
        self.job["percent"] = done * 100 // total
        self.job["phase"] = "sending data"

    def _finish(self, ok: bool, message: str) -> None:
        self.job.update(active=False, phase="done" if ok else "failed", result={"ok": ok, "message": message})
        if ok:
            self.job["percent"] = 100
        self.note("ok" if ok else "error", ("[simulated] " if self.job["simulated"] else "") + message)

    def _run_simulation(self, package, label: str) -> None:
        from sim_headset import SimUpgradeHeadset  # tests/ is added to the path by app.py

        sim = SimUpgradeHeadset(package.raw)

        def progress(done: int, total: int) -> None:
            self._progress(done, total)
            time.sleep(0.003)  # slow enough to watch; a real transfer takes minutes

        try:
            Upgrader(package.raw, sim.connect, log=self._phase, progress=progress, sleep=lambda s: time.sleep(0.2)).run()
            if bytes(sim.received) != package.raw:
                raise UpgradeError("simulated headset received different bytes")
            self._finish(True, f"{label}: the upload sequence completed against a simulated headset. "
                               "Your real headset was not touched.")
        except UpgradeError as err:
            self._finish(False, f"{label}: {err}")

    @staticmethod
    def _set_update_mode(client: MdrClient, voice: bool, enabled: bool, timeout: float = 10.0) -> None:
        if voice:
            client.request(p.vg_set_update_mode(enabled), "VOICE_GUIDANCE_NTFY_STATUS", table=2, timeout=timeout)
        else:
            client.set_fw_update_mode(enabled)

    def _run_real_firmware(self, image: FirmwareImage, label: str) -> None:
        address = self.address
        try:
            with self._lock:
                self._phase("entering firmware update mode")
                client = self._need_client()
                self._set_update_mode(client, False, True)
            with client.serviced_in_background():
                Upgrader(image.raw, lambda: self._gaia_connect(address), log=self._phase,
                         progress=self._progress).run()
            self._finish(True, f"{label}: the headset reported the update complete. Time the hold from off, "
                               "then upload your quiet package again: this file restores Sony's sounds.")
        except (MdrError, UpgradeError, OSError) as err:
            self._finish(False, f"{label}: {err}. Keep it powered and nearby, then reconnect and check the status. "
                                "Pressing upload again with the same file resumes if the headset allows it.")
        finally:
            self._cleanup_after_upload(address, voice=False)

    def _run_real_upload(self, package: VoicePackage, label: str) -> None:
        address = self.address
        try:
            with self._lock:
                self._phase("entering voice package update mode")
                client = self._need_client()
                self._set_update_mode(client, True, True)
            with client.serviced_in_background():
                Upgrader(package.raw, lambda: self._gaia_connect(address), log=self._phase,
                         progress=self._progress).run()
            self._finish(True, f"{label}: the headset reported the update complete.")
        except (MdrError, UpgradeError, OSError) as err:
            self._finish(False, f"{label}: {err}. Do not panic: keep it powered, reconnect, and check the status. "
                                "Sony's original package can be uploaded the same way.")
        finally:
            self._cleanup_after_upload(address, voice=True)

    def _cleanup_after_upload(self, address: str, voice: bool) -> None:
        """The headset reboots mid-update, so the old MDR link is normally gone by now.

        Like the app, leave update mode over the new link too, and ignore failure.
        """
        with self._lock:
            if self.client is not None:
                try:
                    self._set_update_mode(self.client, voice, False, timeout=5.0)
                except (MdrError, OSError):
                    pass
            self.disconnect()
        for attempt in range(RECONNECT_ATTEMPTS):
            try:
                self.connect(address)
                with self._lock:
                    try:
                        self._set_update_mode(self.client, voice, False, timeout=5.0)
                    except (MdrError, OSError) as err:
                        self.note("warn", f"could not leave update mode: {err}")
                return
            except (MdrError, OSError) as err:
                self.note("warn", f"reconnect attempt {attempt + 1} failed: {err}")
                time.sleep(RECONNECT_DELAY_SECONDS)
        self.note("warn", "could not reconnect automatically. Check the headset is on, then press Connect.")


__all__ = ["DeviceService", "FirmwareImage", "discover_paired", "PackageError", "ResumePoint"]
