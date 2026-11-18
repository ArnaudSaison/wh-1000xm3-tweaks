"""Web UI and package tests. Run with: uv run python -m unittest discover -s tests"""
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "flasher"), str(ROOT / "tests"), str(ROOT / "tools")]

from sim_headset import SimMdr, SimUpgradeHeadset  # noqa: E402
from xm3 import protocol as p  # noqa: E402
import button_timing  # noqa: E402
import dfu_sign  # noqa: E402
from xm3.package import Chunk, PackageError, VoicePackage, attenuate, inspect_image, join_chunks, xor16  # noqa: E402

try:
    from app import create_app
    from device import DeviceService
except ImportError:
    create_app = None


def mp3_frame(gain: int) -> bytes:
    """One MPEG-2 Layer III mono frame at 16 kHz, 32 kbit/s (144 bytes)."""
    side = bytearray(9)  # 9-byte MPEG-2 mono side info: global_gain starts at bit 21
    bit = 8 + 1 + 12 + 9
    for i in range(8):
        if gain >> (7 - i) & 1:
            side[(bit + i) // 8] |= 1 << (7 - (bit + i) % 8)
    return bytes.fromhex("fff348c4") + bytes(side) + bytes(131)


def build_image(files: dict[str, bytes]) -> bytes:
    """A minimal CSR 'File' image: a root directory, plain files and the checksum trailer."""
    names = sorted(files)
    n = 1 + len(names)
    header_words = (10 + 12 * n) // 2
    name_blob = bytearray()
    name_offsets = []
    for name in names:
        name_offsets.append(header_words + len(name_blob) // 2)
        name_blob += struct.pack(">H", len(name)) + name.encode("utf-16-be")
    data_start = header_words + len(name_blob) // 2
    data = bytearray()
    entries = [struct.pack(">III", 0x80000000, 2, len(names))]
    for name, offset in zip(names, name_offsets):
        entries.append(struct.pack(">III", offset, data_start + len(data) // 2, len(files[name])))
        data += files[name] + (b"\0" if len(files[name]) % 2 else b"")
    body = b"".join(entries) + name_blob + data
    size_words = (10 + len(body) + 12) // 2
    trailer = b"\x00\x00\x01\x00" + struct.pack(">I", size_words) + b"File"
    image = bytearray(b"File" + struct.pack(">IH", size_words, n) + body + trailer)
    check = xor16(image)
    image[-12:-10] = check.to_bytes(2, "big")
    return bytes(image)


def build_package(language: int = 1, partition: bytes = b"\x00\x03\x00\x01") -> bytes:
    files = {
        "beep/tone.mp3": mp3_frame(100) * 4,
        "beep/tone_alias.mp3": mp3_frame(100) * 2,
        "beep/silent_x.mp3": mp3_frame(100),
        "voice/hello.mp3": mp3_frame(120) * 3,
        "voice/language.bin": bytes([language]),
    }
    header = b"ZARKOV77" + struct.pack(">HHH", 2, 1, 1) + struct.pack(">HH", 0, 0xFFFF)
    chunks = [Chunk(b"APPUHDR2", header), Chunk(b"PARTDATA", partition + build_image(files)), Chunk(b"APPUPFTR", bytes(128))]
    return join_chunks(chunks)


def _read_gain(frame: bytes) -> int:
    bit = 4 * 8 + 8 + 1 + 12 + 9
    value = 0
    for i in range(8):
        value = value << 1 | (frame[(bit + i) // 8] >> (7 - (bit + i) % 8)) & 1
    return value


class Package(unittest.TestCase):
    def test_parses_and_finds_language(self):
        package = VoicePackage(build_package(language=2))
        self.assertEqual(package.language, 2)
        self.assertIn("voice/hello.mp3", package.audio_files)

    def test_attenuation_changes_gain_by_category(self):
        raw, report = attenuate(VoicePackage(build_package()), 6, 2)
        out = VoicePackage(raw)
        self.assertEqual(_read_gain(out.read("voice/hello.mp3")), 114)
        self.assertEqual(_read_gain(out.read("beep/tone.mp3")), 98)
        self.assertEqual(report["voice_db"], -9.0)
        self.assertEqual(report["clipped_granules"], 0)

    def test_silent_files_untouched_and_size_constant(self):
        source = VoicePackage(build_package())
        raw, _ = attenuate(source, 6, 6)
        self.assertEqual(VoicePackage(raw).read("beep/silent_x.mp3"), source.read("beep/silent_x.mp3"))
        self.assertEqual(len(raw), len(source.raw))

    def test_zero_attenuation_reproduces_the_original(self):
        source = VoicePackage(build_package())
        self.assertEqual(attenuate(source, 0, 0)[0], source.raw)

    def test_result_has_valid_checksum(self):
        raw, _ = attenuate(VoicePackage(build_package()), 8, 3)
        self.assertEqual(xor16(VoicePackage(raw).image), 0)

    def test_corrupted_image_rejected(self):
        raw = bytearray(build_package())
        raw[-200] ^= 0xFF
        with self.assertRaisesRegex(PackageError, "checksum"):
            VoicePackage(bytes(raw))

    def test_firmware_rejected_as_voice_package(self):
        header = b"ZARKOV77" + bytes(10)
        raw = join_chunks([Chunk(b"APPUHDR2", header), Chunk(b"PARTDATA", b"\x00\x01" + bytes(20)),
                           Chunk(b"PARTDATA", b"\x00\x03\x00\x01" + bytes(20)), Chunk(b"APPUPFTR", bytes(128))])
        self.assertEqual(inspect_image(raw), [1, 3])
        with self.assertRaisesRegex(PackageError, "not a voice package"):
            VoicePackage(raw)

    def test_wrong_variant_rejected(self):
        raw = bytearray(build_package())
        raw[12:20] = b"OTHERMOD"
        with self.assertRaisesRegex(PackageError, "variant"):
            VoicePackage(bytes(raw))

    def test_truncated_file_rejected(self):
        with self.assertRaises(PackageError):
            VoicePackage(build_package()[:-5])

    def test_attenuation_range_enforced(self):
        with self.assertRaises(PackageError):
            attenuate(VoicePackage(build_package()), 41, 0)


def status_replies(battery=70, language=1, model="WH-1000XM3"):
    def device(kind, text):
        return bytes([0x05, kind, len(text)]) + text.encode()

    def update(kind, text):
        return bytes([0x37, kind, len(text)]) + text.encode()

    def text(value):
        return bytes([len(value)]) + value.encode()

    gs1 = bytes.fromhex("d1d102") + text("ASSIGNABLE_KEY_SETTING") + bytes.fromhex("000202")
    gs1 += b"\x02" + text("ASSIGNABLE_KEY_ELEM_NCASM") + b"\x00" + b"\x02" + text("ASSIGNABLE_KEY_ELEM_AMAZON_ALEXA") + b"\x00"
    gs2 = bytes.fromhex("d1d202") + text("TOUCH_PANEL_SETTING") + b"\x00\x01"
    return {
        p.get_protocol_info(): [bytes.fromhex("01004010")],
        p.get_device_info("MODEL_NAME"): [device(1, model)],
        p.get_device_info("FW_VERSION"): [device(2, "4.5.2")],
        p.get_battery(): [bytes([0x11, 0, battery, 0])],
        p.get_codec(): [bytes.fromhex("190002")],
        p.get_ncasm(): [bytes.fromhex("6702010202010000")],
        p.get_eq(): [bytes.fromhex("570100060a0a0a0a0a0a")],
        p.get_update_param("SERVICE_ID"): [update(3, "MDRID291601")],
        p.get_update_param("NATION_CODE"): [update(4, "Testland")],
        p.get_update_param("BATTERY_POWER_THRESHOLD"): [bytes([0x37, 8, 50])],
        p.get_general_capability(p.GS1): [gs1],
        p.get_general_capability(p.GS2): [gs2],
        p.get_general(p.GS1): [bytes.fromhex("d7d10200")],
        p.get_general(p.GS2): [bytes.fromhex("d7d20101")],
        p.vg_get_onoff(): [bytes.fromhex("47010101")],
        p.vg_get_language(): [bytes([0x47, 1, 2, language])],
        p.vg_set_update_mode(True): [bytes.fromhex("45010200")],
        p.vg_set_update_mode(False): [bytes.fromhex("45010201")],
        p.set_fw_update_mode(True): [bytes.fromhex("350100")],
        p.set_fw_update_mode(False): [bytes.fromhex("350101")],
    }


@unittest.skipIf(create_app is None, "flask not installed")
class WebUi(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name)
        self.mdr = SimMdr(status_replies())
        self.gaia = None
        self.opened_gaia = 0

        def gaia_connect(address):
            self.opened_gaia += 1
            return self.gaia.connect()

        self.service = DeviceService(mdr_connect=lambda address: self.fresh_mdr(), gaia_connect=gaia_connect)
        self.app = create_app(self.data, self.service)
        self.http = self.app.test_client()
        self.token = self.app.config["XM3_TOKEN"]
        (self.data / "packages").mkdir(exist_ok=True)

    def fresh_mdr(self):
        link = self.mdr.link
        return link

    def post(self, path, **body):
        return self.http.post(path, json=body, headers={"X-Token": self.token})

    def add_package(self, name="sony-x", language=1):
        (self.data / "packages" / f"{name}.bin").write_bytes(build_package(language))
        return name

    def wait_job(self):
        deadline = time.monotonic() + 20
        while self.service.job["active"] and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.service.job

    def test_post_without_token_refused(self):
        self.assertEqual(self.http.post("/api/connect", json={"address": "00:00:00:00:00:00"}).status_code, 403)
        self.assertEqual(self.http.post("/api/upload", json={"confirm": "UPLOAD"}).status_code, 403)

    def test_foreign_host_refused(self):
        self.assertEqual(self.http.get("/", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.http.get("/", headers={"Host": "127.0.0.1:8750"}).status_code, 200)

    def test_connect_reads_status_and_sends_only_gets(self):
        self.assertEqual(self.post("/api/connect", address="aa:bb:cc:dd:ee:ff").status_code, 200)
        status = self.http.get("/api/state").get_json()["status"]
        self.assertEqual(status["battery"]["level"], 70)
        self.assertEqual(status["nc"], "Noise cancelling")
        self.assertEqual(status["custom_button"], "Noise control")
        self.assertEqual(status["touch_panel"], "On")
        self.assertEqual(status["service"], "MDRID291601")
        self.assertEqual(status["language"], "English")
        writes = {0x34, 0x22, 0x48, 0x44, 0x58, 0x68, 0xD8, 0xF8}
        table2_gets = {0x46}
        self.assertFalse({m[0] for m in self.mdr.received} & (writes - table2_gets))

    def test_invalid_address_rejected(self):
        self.assertEqual(self.post("/api/connect", address="nonsense").status_code, 400)

    def test_build_and_preview(self):
        self.add_package()
        reply = self.post("/api/build", source="sony-x", voice_steps=4, beep_steps=2)
        self.assertEqual(reply.status_code, 200)
        built = reply.get_json()["id"]
        self.assertTrue(built.startswith("quiet-v4-b2"))
        self.assertEqual(self.http.get(f"/api/audio/{built}/voice/hello.mp3").mimetype, "audio/mpeg")
        self.assertEqual(self.http.get(f"/api/audio/{built}/voice/language.bin").status_code, 404)

    def test_path_traversal_blocked(self):
        self.assertEqual(self.http.get("/api/files/..%2F..%2Fsecret").status_code, 404)
        self.assertEqual(self.post("/api/build", source="../x").status_code, 400)

    def test_simulation_needs_no_headset_and_touches_nothing(self):
        self.add_package()
        self.assertEqual(self.post("/api/simulate", package="sony-x").status_code, 200)
        job = self.wait_job()
        self.assertTrue(job["result"]["ok"], job)
        self.assertTrue(job["simulated"])
        self.assertEqual(self.opened_gaia, 0)
        self.assertEqual(self.mdr.received, [])

    def test_upload_needs_confirmation_word(self):
        self.add_package()
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.post("/api/upload", package="sony-x", confirm="yes").status_code, 400)
        self.assertEqual(self.opened_gaia, 0)

    def test_upload_refused_when_not_connected(self):
        self.add_package()
        reply = self.post("/api/upload", package="sony-x", confirm="UPLOAD")
        self.assertEqual(reply.status_code, 400)
        self.assertIn("Headset connected", reply.get_json()["error"])

    def test_upload_refused_at_exact_battery_threshold(self):
        self.mdr = SimMdr(status_replies(battery=50))
        self.add_package()
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        reply = self.post("/api/upload", package="sony-x", confirm="UPLOAD")
        self.assertEqual(reply.status_code, 400)
        self.assertIn("Battery", reply.get_json()["error"])
        self.assertNotIn(p.vg_set_update_mode(True), self.mdr.received)

    def test_upload_refused_for_wrong_language(self):
        self.add_package(language=2)
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        reply = self.post("/api/upload", package="sony-x", confirm="UPLOAD")
        self.assertEqual(reply.status_code, 400)
        self.assertIn("language", reply.get_json()["error"])

    def test_upload_refused_for_wrong_model(self):
        self.mdr = SimMdr(status_replies(model="WH-1000XM4"))
        self.add_package()
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.post("/api/upload", package="sony-x", confirm="UPLOAD").status_code, 400)

    def test_full_upload_flow_uses_voice_mode_never_firmware_mode(self):
        name = self.add_package()
        raw = build_package()
        self.gaia = SimUpgradeHeadset(raw)
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        self.service._gaia_connect = lambda address: self.gaia.connect()
        original_upgrader = sys.modules["device"].Upgrader

        def fast(image, connect, **kw):
            return original_upgrader(image, connect, sleep=lambda s: None, reboot_timeout=1.0, **kw)

        sys.modules["device"].Upgrader = fast
        self.addCleanup(setattr, sys.modules["device"], "Upgrader", original_upgrader)
        self.assertEqual(self.post("/api/upload", package=name, confirm="UPLOAD").status_code, 200)
        job = self.wait_job()
        self.assertTrue(job["result"]["ok"], job)
        self.assertEqual(bytes(self.gaia.received), raw)
        self.assertTrue(self.gaia.committed)
        self.assertIn(p.vg_set_update_mode(True), self.mdr.received)
        self.assertNotIn(p.set_fw_update_mode(True), self.mdr.received)
        deadline = time.monotonic() + 5
        while self.mdr.received.count(p.vg_set_update_mode(False)) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.mdr.received.count(p.vg_set_update_mode(False)), 2)  # old link, then new


SONY_DIR = ROOT / "firmware"


class StartsOnItsOwn(unittest.TestCase):
    def test_app_imports_without_the_test_suites_path(self):
        result = subprocess.run([sys.executable, str(ROOT / "flasher" / "app.py"), "--help"],
                                capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipIf(create_app is None, "flask not installed")
@unittest.skipUnless((SONY_DIR / "HDX-2916_V0452_SPLC_with_stack.bin").exists(), "firmware not downloaded")
class FirmwareUi(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.mdr = SimMdr(status_replies())
        self.gaia = None
        self.opened_gaia = 0

        def gaia_connect(address):
            self.opened_gaia += 1
            return self.gaia.connect()

        self.service = DeviceService(mdr_connect=lambda address: self.mdr.link, gaia_connect=gaia_connect)
        self.app = create_app(Path(self.folder.name), self.service, SONY_DIR)
        self.http = self.app.test_client()
        self.token = self.app.config["XM3_TOKEN"]

    def post(self, path, **body):
        return self.http.post(path, json=body, headers={"X-Token": self.token})

    def build(self, **timings):
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        reply = self.post("/api/firmware/build", timings=timings or {"long_ms": 4000})
        self.assertEqual(reply.status_code, 200, reply.get_json())
        return reply.get_json()

    def checks(self, image_id):
        return {c["id"]: c["ok"] for c in self.http.get(f"/api/firmware/preflight?id={image_id}").get_json()["checks"]}

    def test_build_needs_a_connected_headset(self):
        self.assertIn("connect", self.post("/api/firmware/build", timings={"long_ms": 4000}).get_json()["error"])

    def test_build_uses_the_requested_time_and_the_headsets_region(self):
        entry = self.build()
        self.assertEqual(entry["timings"]["long_ms"], 4000)
        self.assertEqual(entry["service"], "MDRID291601")
        raw = (Path(self.folder.name) / "firmware" / f"{entry['id']}.bin").read_bytes()
        source = (SONY_DIR / "HDX-2916_V0452_SPLC_with_stack.bin").read_bytes()
        self.assertEqual(raw, dfu_sign.patch(source, dfu_sign.pairing_edits(source, 4000)))
        self.assertTrue(all(self.checks(entry["id"]).values()))

    def test_double_press_option_builds_a_file_that_carries_the_patch(self):
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        reply = self.post("/api/firmware/build", timings={"short_ms": 1000, "long_ms": 3000}, double_press=True)
        self.assertEqual(reply.status_code, 200, reply.get_json())
        entry = reply.get_json()
        self.assertTrue(entry["id"].startswith("doublepress-"))
        self.assertTrue(entry["double_press"])
        self.assertIn("Double press for pairing, short hold 1 s, long hold 3 s", entry["label"])
        self.assertTrue(all(row["ok"] for row in entry["rehearsal"]))
        self.assertEqual(sum(row["label"].startswith("Double press") for row in entry["rehearsal"]), 4)
        raw = (Path(self.folder.name) / "firmware" / f"{entry['id']}.bin").read_bytes()
        source = (SONY_DIR / "HDX-2916_V0452_SPLC_with_stack.bin").read_bytes()
        self.assertEqual(raw, button_timing.build(source, {"short_ms": 1000, "long_ms": 3000}, True))
        self.assertTrue(all(self.checks(entry["id"]).values()))

    def test_without_the_option_the_file_has_no_patch(self):
        entry = self.build(short_ms=1000, long_ms=3000)
        self.assertTrue(entry["id"].startswith("timing-"))
        self.assertFalse(entry["double_press"])
        self.assertTrue(any("without the option" in row["label"] and row["ok"] for row in entry["rehearsal"]))

    def test_the_option_must_be_a_real_true(self):
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        reply = self.post("/api/firmware/build", timings={"long_ms": 4000}, double_press="yes")
        self.assertTrue(reply.get_json()["id"].startswith("timing-"))

    def test_page_offers_the_option_and_the_check_explains_it(self):
        listing = self.http.get("/api/firmware/timings").get_json()
        self.assertEqual([f["id"] for f in listing["features"]], ["double_press"])
        self.assertEqual(listing["double_press_window_ms"], 500)
        rows = self.post("/api/firmware/check", timings={}, double_press=True).get_json()["rows"]
        self.assertNotIn("error", {row["level"] for row in rows})
        self.assertIn("error", {row["level"] for row in self.post("/api/firmware/check", timings={}).get_json()["rows"]})
        page = self.http.get("/").get_data(as_text=True)
        self.assertIn('id="featureControls"', page)
        self.assertIn("double_press:doublePress()", page)

    def test_out_of_range_time_refused(self):
        self.post("/api/connect", address="AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.post("/api/firmware/build", timings={"long_ms": 1000}).status_code, 400)

    def test_upload_needs_its_own_word_and_is_not_a_voice_package(self):
        entry = self.build()
        self.assertEqual(self.post("/api/firmware/upload", id=entry["id"], confirm="UPLOAD").status_code, 400)
        self.assertEqual(self.post("/api/upload", package=entry["id"], confirm="UPLOAD").status_code, 400)
        self.assertEqual(self.opened_gaia, 0)
        self.assertNotIn(p.set_fw_update_mode(True), self.mdr.received)

    def test_upload_refused_for_wrong_version(self):
        entry = self.build()
        self.service.status["firmware"] = "4.1.1"
        self.assertFalse(self.checks(entry["id"])["version"])
        self.assertEqual(self.post("/api/firmware/upload", id=entry["id"], confirm="FLASH").status_code, 400)
        self.assertEqual(self.opened_gaia, 0)

    def test_upload_refused_for_low_battery(self):
        entry = self.build()
        self.service.status["battery"]["level"] = 50
        self.assertEqual(self.post("/api/firmware/upload", id=entry["id"], confirm="FLASH").status_code, 400)
        self.assertEqual(self.opened_gaia, 0)

    def test_upload_refused_for_another_regions_file(self):
        entry = self.build()
        self.service.status["service"] = "MDRID291600"
        self.assertEqual(self.post("/api/firmware/upload", id=entry["id"], confirm="FLASH").status_code, 400)
        self.assertEqual(self.opened_gaia, 0)

    def test_full_flow_uses_firmware_mode_and_leaves_it(self):
        entry = self.build()
        raw = (Path(self.folder.name) / "firmware" / f"{entry['id']}.bin").read_bytes()
        self.gaia = SimUpgradeHeadset(raw)
        original_upgrader = sys.modules["device"].Upgrader

        def fast(image, connect, **kw):
            return original_upgrader(image, connect, sleep=lambda s: None, reboot_timeout=1.0, **kw)

        sys.modules["device"].Upgrader = fast
        self.addCleanup(setattr, sys.modules["device"], "Upgrader", original_upgrader)
        self.assertEqual(self.post("/api/firmware/upload", id=entry["id"], confirm="FLASH").status_code, 200)
        deadline = time.monotonic() + 120
        while self.service.job["active"] and time.monotonic() < deadline:
            time.sleep(0.05)
        job = self.service.job
        self.assertTrue(job["result"]["ok"], job)
        self.assertTrue(job["firmware"])
        self.assertEqual(bytes(self.gaia.received), raw)
        self.assertTrue(self.gaia.committed)
        self.assertIn(p.set_fw_update_mode(True), self.mdr.received)
        self.assertNotIn(p.vg_set_update_mode(True), self.mdr.received)
        deadline = time.monotonic() + 5
        while self.mdr.received.count(p.set_fw_update_mode(False)) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.mdr.received.count(p.set_fw_update_mode(False)), 2)


if __name__ == "__main__":
    unittest.main()
