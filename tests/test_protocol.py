"""Run with: uv run python -m unittest discover -s tests"""
import hashlib
import os
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sim_headset import SimMdr, SimUpgradeHeadset  # noqa: E402
from xm3 import protocol as p  # noqa: E402
from xm3 import tables as t  # noqa: E402
from xm3.client import MdrClient, MdrError  # noqa: E402
from xm3.frames import Frame, FrameError, FrameParser, ack_for, decode  # noqa: E402
from xm3.gaia import (  # noqa: E402
    CMD_VM_UPGRADE_CONTROL, VENDOR_QUALCOMM, VENDOR_SONY, GaiaPacket, GaiaParser, ResumePoint, VmuOp, vmu_decode,
    vmu_encode,
)
from xm3.upgrade import Upgrader, UpgradeError, sync_id  # noqa: E402


class Frames(unittest.TestCase):
    def test_ack_vectors_hardcoded_in_the_app(self):
        self.assertEqual(ack_for(1).encode().hex(), "3e0100000000000" "13c")
        self.assertEqual(ack_for(0).encode().hex(), "3e0101000000000" "23c")

    def test_published_capture_of_the_protocol_probe(self):
        frame = Frame(0x0C, 0, bytes([0, 0]))
        self.assertEqual(frame.encode().hex(), "3e0c00000000020000" "0e3c")

    def test_escaping_of_every_reserved_byte(self):
        payload = bytes([0x3C, 0x3D, 0x3E, 0x2C, 0x2D, 0x2E])
        wire = Frame(0x0C, 1, payload).encode()
        self.assertIn(bytes([0x3D, 0x2C, 0x3D, 0x2D, 0x3D, 0x2E, 0x2C, 0x2D, 0x2E]), wire)
        self.assertNotIn(0x3C, wire[1:-1])
        self.assertNotIn(0x3E, wire[1:-1])
        self.assertEqual(decode(wire[1:-1]).payload, payload)

    def test_checksum_is_over_unescaped_bytes(self):
        wire = Frame(0x0C, 0, bytes([0x3E])).encode()
        self.assertEqual(wire, bytes.fromhex("3e0c0000000001") + bytes([0x3D, 0x2E]) + bytes([0x0C + 1 + 0x3E]) + b"\x3c")

    def test_bad_checksum_rejected_and_parser_continues(self):
        good = Frame(0x0C, 0, b"\x10\x00").encode()
        bad = bytearray(good)
        bad[-2] ^= 1
        parser = FrameParser()
        frames = list(parser.feed(bytes(bad) + good))
        self.assertEqual([f.payload for f in frames], [b"\x10\x00"])
        self.assertIsInstance(parser.errors[0], FrameError)

    def test_parser_handles_byte_by_byte_and_junk(self):
        wire = b"junk" + Frame(0x0E, 1, b"\x46\x01\x01").encode()
        parser = FrameParser()
        frames = [f for i in range(len(wire)) for f in parser.feed(wire[i:i + 1])]
        self.assertEqual(frames, [Frame(0x0E, 1, b"\x46\x01\x01")])


class Tables(unittest.TestCase):
    """Values whose encoding was indirect in the decompiled source (BSON constants)."""

    def test_codes(self):
        self.assertEqual(t.DATA_TYPE["DATA_MDR"], 0x0C)
        self.assertEqual(t.DATA_TYPE["DATA_MDR_NO2"], 0x0E)
        self.assertEqual(t.T1_COMMAND["COMMON_GET_BATTERY_LEVEL"], 0x10)
        self.assertEqual(t.T1_COMMAND["COMMON_RET_BATTERY_LEVEL"], 0x11)
        self.assertEqual(t.T1_COMMAND["TEST_COMMAND"], 0xFF)
        self.assertEqual(t.T1_COMMAND["SYSTEM_SET_PARAM"], 0xF8)
        self.assertEqual(t.T2_COMMAND["VOICE_GUIDANCE_SET_PARAM"], 0x48)
        self.assertEqual(t.ASSIGNABLE_PRESET["NO_FUNCTION"], 0xFF)


class Payloads(unittest.TestCase):
    def test_exchange_published_for_the_xm3(self):
        self.assertEqual(p.get_ncasm().hex(), "6602")
        parsed = p.parse_ncasm(bytes.fromhex("6702010202010000"))
        self.assertEqual(parsed["effect"], "ON")
        self.assertEqual(parsed["nc_value"], 2)
        self.assertEqual(p.set_ncasm(0x11, 1, 2, 1, 0, 0).hex(), "6802110102010000")

    def test_update_mode_status_is_inverted(self):
        self.assertEqual(p.set_fw_update_mode(True).hex(), "340100")
        self.assertEqual(p.set_fw_update_mode(False).hex(), "340101")

    def test_assignable_capability_nested_parse(self):
        raw = bytes([0xF1, 0x06, 1,  # one key
                     2, 1, 0x00, 2,  # CUSTOM_KEY, BUTTON, default preset, two presets
                     0x00, 1, 0x00, 0x01,  # first preset: one action
                     0x30, 1, 0x00, 0x34])  # second preset: one action
        keys = p.parse_assignable_capability(raw)
        self.assertEqual(keys[0].key, "CUSTOM_KEY")
        self.assertEqual(keys[0].key_type, "BUTTON")
        self.assertEqual(keys[0].presets[0].actions, {"SINGLE_TAP": "NC_ASM_OFF"})
        self.assertEqual(keys[0].presets[1].preset, "VOICE_RECOGNITION")

    def test_general_setting_replies_captured_from_a_real_xm3(self):
        def text(s):
            return bytes([len(s)]) + s.encode()

        raw = bytes.fromhex("d1d102") + text("ASSIGNABLE_KEY_SETTING") + bytes.fromhex("00 02 03")
        raw += b"\x02" + text("ASSIGNABLE_KEY_ELEM_NCASM") + b"\x00"
        raw += b"\x02" + text("ASSIGNABLE_KEY_ELEM_GOOGLE_ASSISTANT") + text("ASSIGNABLE_KEY_ELEM_GOOGLE_ASSISTANT_SUMMARY")
        raw += b"\x02" + text("ASSIGNABLE_KEY_ELEM_AMAZON_ALEXA") + text("ASSIGNABLE_KEY_ELEM_AMAZON_ALEXA_SUMMARY")
        info = p.parse_general_capability(raw)
        self.assertEqual(info["title"], "ASSIGNABLE_KEY_SETTING")
        self.assertEqual(len(info["options"]), 3)
        self.assertEqual(info["options"][2], "ASSIGNABLE_KEY_ELEM_AMAZON_ALEXA")
        touch = p.parse_general_capability(bytes.fromhex("d1d202") + text("TOUCH_PANEL_SETTING") + b"\x00\x01")
        self.assertEqual((touch["title"], touch["type"]), ("TOUCH_PANEL_SETTING", "boolean"))
        self.assertEqual(p.parse_general(bytes.fromhex("d7d10200")), {"type": "list", "value": 0})
        self.assertEqual(p.parse_general(bytes.fromhex("d7d20101")), {"type": "boolean", "value": 1})
        self.assertEqual(p.get_general_capability(p.GS1).hex(), "d0d101")

    def test_voice_guidance_update_mode(self):
        self.assertEqual(p.vg_set_update_mode(True).hex(), "44010200")
        self.assertEqual(p.vg_set_update_mode(False).hex(), "44010201")

    def test_set_assignable(self):
        self.assertEqual(p.set_assignable([0x30]).hex(), "f8060130")

    def test_battery(self):
        self.assertEqual(p.parse_battery(bytes([0x11, 0, 70, 1])), {"level": 70, "charging": "CHARGING"})


class Client(unittest.TestCase):
    def client(self, replies, **kw):
        sim = SimMdr(replies, **kw)
        return sim, MdrClient(sim.link, ack_timeout=0.01, retries=2)

    def test_handshake_and_request_acks_replies(self):
        sim, client = self.client({p.get_protocol_info(): [bytes.fromhex("01007000")]})
        self.assertEqual(client.handshake(), 0x7000)
        self.assertEqual(sim.acks_from_client, 1)

    def test_retransmits_until_acked(self):
        sim, client = self.client({p.get_battery(): [bytes([0x11, 0, 50, 0])]}, drop_first_acks=2)
        self.assertEqual(client.battery()["level"], 50)

    def test_gives_up_when_never_acked(self):
        _, client = self.client({}, drop_first_acks=99)
        with self.assertRaises(MdrError):
            client.send(p.get_battery())

    def test_sequence_alternates_across_requests(self):
        replies = {p.get_battery(): [bytes([0x11, 0, 50, 0])], p.get_codec(): [bytes([0x19, 0, 1])]}
        sim, client = self.client(replies)
        client.request(p.get_battery(), "COMMON_RET_BATTERY_LEVEL")
        client.request(p.get_codec(), "COMMON_RET_AUDIO_CODEC")
        self.assertEqual(sim.received, [p.get_battery(), p.get_codec()])

    def test_duplicate_incoming_frame_delivered_once(self):
        sim, client = self.client({})
        frame = Frame(0x0C, 0, bytes([0x13, 0, 40, 0])).encode()
        sim.link.reply(frame)
        sim.link.reply(frame)
        sim.link.reply(Frame(0x0C, 1, bytes([0x13, 0, 41, 0])).encode())
        first = client.wait_for(0x13, timeout=0.05)
        second = client.wait_for(0x13, timeout=0.05)
        self.assertEqual((first[2], second[2]), (40, 41))

    def test_notifications_are_acknowledged_during_background_service(self):
        sim, client = self.client({})
        with client.serviced_in_background():
            sim.link.reply(Frame(0x0C, 0, bytes([0x13, 0, 40, 0])).encode())
            deadline = time.monotonic() + 2
            while sim.acks_from_client == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(sim.acks_from_client, 1)

    def test_background_service_survives_a_lost_link(self):
        sim, client = self.client({})
        sim.link.recv = lambda timeout: b""  # headset rebooted
        with client.serviced_in_background():
            time.sleep(0.05)

    def test_update_mode_refusal(self):
        reply = {p.set_fw_update_mode(True): [bytes([0x35, 0x01, 0x01])]}
        _, client = self.client(reply)
        with self.assertRaises(MdrError):
            client.set_fw_update_mode(True)


class Gaia(unittest.TestCase):
    def test_vector_sony_vendor_command(self):
        self.assertEqual(GaiaPacket(VENDOR_SONY, 0x0200, b"\x02").encode().hex(), "ff010001012d020002")

    def test_vector_with_checksum_flag(self):
        wire = GaiaPacket(VENDOR_QUALCOMM, 0x0640, flags=1).encode()
        check = 0
        for byte in wire[:-1]:
            check ^= byte
        self.assertEqual(wire[-1], check)
        self.assertEqual(list(GaiaParser().feed(wire))[0].command, 0x0640)

    def test_ack_bit(self):
        packet = GaiaPacket(VENDOR_QUALCOMM, 0x8642, b"\x00")
        self.assertTrue(packet.is_ack)
        self.assertEqual(packet.base_command, CMD_VM_UPGRADE_CONTROL)

    def test_parser_reassembles_fragments_and_resyncs(self):
        wire = b"\x00\x01" + GaiaPacket(VENDOR_QUALCOMM, 0x4003, b"\x12\x03\x00\x00").encode()
        parser = GaiaParser()
        packets = [x for i in range(len(wire)) for x in parser.feed(wire[i:i + 1])]
        self.assertEqual(len(packets), 1)
        self.assertEqual(vmu_decode(packets[0].payload[1:]), (0x03, b""))

    def test_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            GaiaPacket(VENDOR_QUALCOMM, 0x0642, bytes(255)).encode()

    def test_largest_data_packet_fits_a_gaia_frame(self):
        wire = vmu_encode(VmuOp.DATA, bytes(251))
        self.assertLessEqual(len(wire), 254)


class Upgrade(unittest.TestCase):
    IMAGE = b"APPUHDR2" + os.urandom(5003)

    def run_upgrade(self, sim, image=None, **kw):
        kw.setdefault("reboot_timeout", 1.0)
        kw.setdefault("chunk_timeout", 0.01)
        kw.setdefault("install_timeout", 0.01)
        Upgrader(image or self.IMAGE, sim.connect, log=lambda s: None, sleep=lambda s: None, **kw).run()

    def test_sync_id_is_last_four_md5_bytes(self):
        self.assertEqual(sync_id(self.IMAGE), hashlib.md5(self.IMAGE).digest()[12:])

    def test_full_flow_delivers_exact_image_and_commits(self):
        sim = SimUpgradeHeadset(self.IMAGE)
        self.run_upgrade(sim)
        self.assertEqual(bytes(sim.received), self.IMAGE)
        self.assertTrue(sim.last_flag_seen)
        self.assertEqual(sim.connections, 2)
        self.assertTrue(sim.rebooted and sim.committed and sim.installed)
        self.assertEqual(sim.sync_ids, [sync_id(self.IMAGE)] * 2)

    def test_reboot_trigger_is_not_waited_on(self):
        sim = SimUpgradeHeadset(self.IMAGE)
        self.assertTrue(sim.reboot_without_ack)
        self.run_upgrade(sim)  # would time out if the client waited for that ACK
        self.assertTrue(sim.rebooted and sim.committed)

    def test_chunks_capped_to_250_even_if_headset_asks_for_more(self):
        sim = SimUpgradeHeadset(self.IMAGE, chunk=4000)
        sizes = []
        real = sim._on_vmu

        def spy(op, data):
            if op == VmuOp.DATA:
                sizes.append(len(data) - 1)
            real(op, data)

        sim._on_vmu = spy
        self.run_upgrade(sim)
        self.assertEqual(max(sizes), 250)
        self.assertEqual(bytes(sim.received), self.IMAGE)

    def test_offset_skip_resumes_a_partial_transfer(self):
        sim = SimUpgradeHeadset(self.IMAGE, skip_at_start=1000)
        self.run_upgrade(sim)
        self.assertEqual(bytes(sim.received), self.IMAGE)

    def test_install_without_commit_request(self):
        sim = SimUpgradeHeadset(self.IMAGE, commit=False)
        self.run_upgrade(sim)
        self.assertTrue(sim.installed)
        self.assertFalse(sim.committed)

    def test_resume_after_interrupted_reboot_goes_straight_to_install(self):
        sim = SimUpgradeHeadset(self.IMAGE, resume=ResumePoint.IN_PROGRESS)
        self.run_upgrade(sim)
        self.assertEqual(sim.connections, 1)
        self.assertEqual(len(sim.received), 0)
        self.assertTrue(sim.committed)

    def test_recoverable_sync_error_aborts_and_resyncs_once(self):
        sim = SimUpgradeHeadset(self.IMAGE, sync_errors=[0x0081])
        self.run_upgrade(sim)
        self.assertEqual(sim.aborts, 1)
        self.assertEqual(bytes(sim.received), self.IMAGE)

    def test_second_recoverable_error_fails(self):
        sim = SimUpgradeHeadset(self.IMAGE, sync_errors=[0x0023, 0x0023])
        with self.assertRaises(UpgradeError):
            self.run_upgrade(sim)

    def test_other_sync_error_fails_without_abort(self):
        sim = SimUpgradeHeadset(self.IMAGE, sync_errors=[0x0050])
        with self.assertRaises(UpgradeError):
            self.run_upgrade(sim)
        self.assertEqual(sim.aborts, 0)

    def test_validation_rejection_stops_before_any_reboot(self):
        sim = SimUpgradeHeadset(self.IMAGE, reject_validation=True)
        with self.assertRaises(UpgradeError) as caught:
            self.run_upgrade(sim)
        self.assertIn("rejected the image", str(caught.exception))
        self.assertFalse(sim.rebooted)
        self.assertFalse(sim.installed)

    def test_unsupported_resume_point_fails_and_closes_link(self):
        sim = SimUpgradeHeadset(self.IMAGE, resume=ResumePoint.COMMIT)
        with self.assertRaises(UpgradeError):
            self.run_upgrade(sim)
        self.assertTrue(sim.link.closed)

    def test_headset_never_returning_after_reboot_is_reported(self):
        sim = SimUpgradeHeadset(self.IMAGE)
        attempts = []

        def connect():
            attempts.append(1)
            if len(attempts) > 1:
                raise OSError("host down")
            return sim.connect()

        with self.assertRaises(UpgradeError) as caught:
            Upgrader(self.IMAGE, connect, log=lambda s: None, sleep=lambda s: None, reboot_timeout=0.02).run()
        self.assertIn("did not come back", str(caught.exception))


class Btsnoop(unittest.TestCase):
    def test_decodes_mdr_from_a_synthetic_log(self):
        import contextlib
        import io
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import btsnoop_mdr

        def acl_packet(data: bytes, dlci: int = 5) -> bytes:
            rfcomm = bytes([dlci << 2 | 1, 0xEF, len(data) << 1 | 1]) + data + b"\x00"
            l2cap = struct.pack("<HH", len(rfcomm), 0x0040) + rfcomm
            return b"\x02" + struct.pack("<HH", 0x2001, len(l2cap)) + l2cap

        frame = Frame(0x0C, 0, bytes([0x10, 0x00])).encode()
        packet = acl_packet(frame)
        log = b"btsnoop\0" + struct.pack(">II", 1, 1002)
        log += struct.pack(">IIIIQ", len(packet), len(packet), 0, 0, 0) + packet
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log"
            path.write_bytes(log)
            argv, sys.argv = sys.argv, ["btsnoop_mdr", str(path)]
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    btsnoop_mdr.main()
            finally:
                sys.argv = argv
        self.assertIn("COMMON_GET_BATTERY_LEVEL", out.getvalue())
        self.assertIn("->", out.getvalue())


if __name__ == "__main__":
    unittest.main()
