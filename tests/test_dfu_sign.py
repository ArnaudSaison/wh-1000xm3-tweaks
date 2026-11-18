"""Signature model tests. Real-firmware tests run only if the firmware is present locally."""
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

sys.path.insert(0, str(ROOT))

import dfu_sign  # noqa: E402
from xm3.__main__ import leave_update_mode  # noqa: E402
from xm3.client import MdrError  # noqa: E402
from xm3.package import PackageError, check_firmware  # noqa: E402

FIRMWARE = ROOT / "firmware" / "HDX-2916_V0452_SPLC_with_stack.bin"
GLOBAL_FIRMWARE = ROOT / "firmware" / "HDX-2916_V0452_with_stack.bin"


class Digest(unittest.TestCase):
    WORDS = list(range(1000, 1200))

    def changed(self, position):
        words = list(self.WORDS)
        words[position] ^= 1
        return dfu_sign.digest(words) != dfu_sign.digest(self.WORDS)

    def test_only_words_0_to_16_of_each_block_are_covered(self):
        for position in (0, 5, 16, 32, 48):
            self.assertTrue(self.changed(position), position)
        for position in (17, 20, 31, 49, 63):
            self.assertFalse(self.changed(position), position)

    def test_is_covered_agrees_with_the_digest(self):
        for position in range(len(self.WORDS)):
            self.assertEqual(dfu_sign.is_covered(position), self.changed(position), position)

    def test_length_is_covered(self):
        self.assertNotEqual(dfu_sign.digest(self.WORDS), dfu_sign.digest(self.WORDS + [0]))

    def test_output_is_below_a_top_bit_modulus(self):
        self.assertEqual(len(dfu_sign.digest(self.WORDS)), dfu_sign.RSA_WORDS)
        self.assertLess(dfu_sign.digest(self.WORDS)[0], 0x8000)


class LeaveUpdateMode(unittest.TestCase):
    class DeadClient:
        def request(self, *args, **kwargs):
            raise MdrError("gone")

        def set_fw_update_mode(self, enabled):
            raise OSError("gone")

    def test_failure_is_reported_not_raised(self):
        def reconnect():
            raise OSError("not back yet")

        self.assertFalse(leave_update_mode(self.DeadClient(), False, reconnect, sleep=lambda s: None))


class SmallRsa(unittest.TestCase):
    def test_cube_matches_integer_power(self):
        modulus = (1 << 1023) + 1155
        signature = [0] * 62 + [0x0001, 0x0002]
        value = 0x00010002
        result = dfu_sign.cube(signature, modulus)
        self.assertEqual(int("".join(f"{w:04x}" for w in result), 16), pow(value, 3, modulus))


@unittest.skipUnless(FIRMWARE.exists(), "firmware not downloaded")
class RealFirmware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = FIRMWARE.read_bytes()

    def test_sony_signatures_verify(self):
        self.assertTrue(all(dfu_sign.verify(self.image).values()))

    def test_flipping_a_covered_filesystem_word_fails(self):
        s = dfu_sign.parse_sections(self.image)
        broken = bytearray(self.image)
        broken[s.fs[0] + 2 * dfu_sign.FS_HASH_START + 2 * 3] ^= 1
        self.assertFalse(dfu_sign.verify(bytes(broken))["filesystem"])

    def test_patch_edits_only_expected_words_and_verifies(self):
        out = dfu_sign.patch(self.image, {0x3772D: 200})
        self.assertEqual(len(out), len(self.image))
        self.assertTrue(all(dfu_sign.verify(out).values()))
        s = dfu_sign.parse_sections(out)
        words = dfu_sign.words_le(out[s.fs[0]:s.fs[0] + s.fs[1]])
        layout = dfu_sign.VmLayout(words)
        self.assertEqual(words[layout.start + 0x3772D], 200)
        difference = sum(a != b for a, b in zip(self.image, out))
        self.assertLessEqual(difference, 20)

    def test_pairing_table_moves_and_only_that_changes(self):
        out = dfu_sign.patch(self.image, dfu_sign.pairing_edits(self.image, 3000))
        self.assertTrue(all(dfu_sign.verify(out).values()))
        s = dfu_sign.parse_sections(out)
        new = dfu_sign.words_le(out[s.fs[0]:s.fs[0] + s.fs[1]])
        old = dfu_sign.words_le(self.image[s.fs[0]:s.fs[0] + s.fs[1]])
        layout = dfu_sign.VmLayout(new)
        vm = new[layout.start:layout.end]
        pointer = vm[dfu_sign.PAIRING_DESCRIPTOR]
        table = vm[(pointer + dfu_sign.VM_DATA_DELTA):(pointer + dfu_sign.VM_DATA_DELTA) + 12]
        self.assertEqual(table, [1500, 0, 0x13, 1500, 1, 0x14, 3000, 0, 0x15, 3000, 1, 0x16])
        self.assertEqual(vm[dfu_sign.PAIRING_DESCRIPTOR + 1], 4)
        self.assertEqual(vm[dfu_sign.PAIRING_TABLE:dfu_sign.PAIRING_TABLE + 12],
                         old[layout.start + dfu_sign.PAIRING_TABLE:layout.start + dfu_sign.PAIRING_TABLE + 12])
        changed = [i - layout.start for i, (a, b) in enumerate(zip(old, new)) if a != b]
        allowed = {dfu_sign.PAIRING_DESCRIPTOR, dfu_sign.VM_CHECKSUM_WORD,
                   *range(dfu_sign.PAIRING_NEW_TABLE, dfu_sign.PAIRING_NEW_TABLE + 12)}
        extra = [w for w in changed if w not in allowed]
        self.assertLessEqual(len(extra), dfu_sign.FILLER_WORDS + 1)  # CRC filler and the image checksum

    def test_filler_uses_only_debug_source_names(self):
        words = dfu_sign.words_le(self.image[slice(*self._fs())])
        layout = dfu_sign.VmLayout(words)
        at = dfu_sign._find_compensation(words, layout)
        start = at
        while 32 <= words[start - 1] < 127:
            start -= 1
        self.assertTrue(dfu_sign._text_at(words, start).endswith(".c"))

    def _fs(self):
        s = dfu_sign.parse_sections(self.image)
        return s.fs[0], s.fs[0] + s.fs[1]

    def test_pairing_rejects_out_of_range_and_wrong_firmware(self):
        for value in (1000, 20000):
            with self.assertRaises(dfu_sign.SignError):
                dfu_sign.pairing_edits(self.image, value)
        s = dfu_sign.parse_sections(self.image)
        words = dfu_sign.words_le(self.image[s.fs[0]:s.fs[0] + s.fs[1]])
        position = s.fs[0] + 2 * (dfu_sign.VmLayout(words).start + dfu_sign.PAIRING_TABLE + 6)
        changed = bytearray(self.image)
        changed[position] ^= 1
        with self.assertRaisesRegex(dfu_sign.SignError, "expected firmware"):
            dfu_sign.pairing_edits(bytes(changed), 3000)

    def test_firmware_check_accepts_a_patch_for_the_same_region(self):
        check_firmware(self.image, "MDRID291601")
        check_firmware(dfu_sign.patch(self.image, dfu_sign.pairing_edits(self.image, 3000)), "MDRID291601")

    def test_firmware_check_refuses_other_region_unknown_service_and_tampering(self):
        with self.assertRaisesRegex(PackageError, "wrong region"):
            check_firmware(self.image, "MDRID291600")
        with self.assertRaisesRegex(PackageError, "unknown service"):
            check_firmware(self.image, "MDRID999999")
        s = dfu_sign.parse_sections(self.image)
        broken = bytearray(self.image)
        broken[s.aps[0]] ^= 1
        with self.assertRaisesRegex(PackageError, "check failed"):
            check_firmware(bytes(broken), "MDRID291601")

    @unittest.skipUnless(GLOBAL_FIRMWARE.exists(), "global firmware not downloaded")
    def test_firmware_check_recognises_the_global_file(self):
        image = GLOBAL_FIRMWARE.read_bytes()
        check_firmware(image, "MDRID291600")
        with self.assertRaisesRegex(PackageError, "wrong region"):
            check_firmware(image, "MDRID291601")

    def test_patch_refuses_covered_word(self):
        with self.assertRaisesRegex(dfu_sign.SignError, "covered"):
            dfu_sign.patch(self.image, {0x37721: 1000})

    def test_patch_refuses_checksum_word_and_bad_value(self):
        with self.assertRaises(dfu_sign.SignError):
            dfu_sign.patch(self.image, {dfu_sign.VM_CHECKSUM_WORD: 1})
        with self.assertRaises(dfu_sign.SignError):
            dfu_sign.patch(self.image, {0x3772D: 0x10000})

    def test_patch_refuses_tampered_input(self):
        s = dfu_sign.parse_sections(self.image)
        broken = bytearray(self.image)
        broken[s.aps[0]] ^= 1
        with self.assertRaisesRegex(dfu_sign.SignError, "does not verify"):
            dfu_sign.patch(bytes(broken), {0x3772D: 200})


if __name__ == "__main__":
    unittest.main()
