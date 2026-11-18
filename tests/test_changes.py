"""Lock the change-analysis findings. Skipped where the git-ignored firmware is absent."""
import struct
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import xap_emulator as xe  # noqa: E402

FW = next((p for p in [HERE.parent / "firmware" / "HDX-2916_V0452_SPLC_with_stack.bin"] if p.exists()), None)
VM = next((p for p in [HERE.parent / "work" / "fw" / "p1" / "fs0" / "vm.app"] if p.exists()), None)


@unittest.skipUnless(VM, "vm.app absent")
class TopBitEntries(unittest.TestCase):
    def test_a_top_bit_entry_is_a_repeat_timer_from_the_press_not_a_double_press(self):
        """Such an entry on input 1 would send the pairing event during any ordinary press."""
        data = VM.read_bytes()
        vm = list(struct.unpack(f">{len(data) // 2}H", data))
        table = [1500, 0, 0x13, 1500, 1, 0x14, 4000, 0, 0x15, 4000, 1, 0x16, 0x8000 | 300, 0, 0x15]
        for i, v in enumerate(table):
            vm[0x394E9 + i] = v
        vm[0x3776E] = (0x394E9 - xe.DELTA) & 0xFFFF
        vm[0x3776F] = 5
        self.assertIn((300, 0x15), xe.schedule_for_input(vm, 1))


@unittest.skipUnless(FW, "firmware absent")
class VolumeCap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dfu_sign
        cls.d = dfu_sign
        cls.img = FW.read_bytes()

    def test_raising_an_uncovered_cap_constant_keeps_signatures_valid(self):
        out = self.d.patch(self.img, {0x17FD4: 0xFF84})  # 0xd2 clamp-compare -> 0xff, uncovered
        self.assertTrue(all(self.d.verify(out).values()))

    def test_a_covered_cap_constant_cannot_be_changed_over_the_air(self):
        with self.assertRaisesRegex(self.d.SignError, "covered"):
            self.d.patch(self.img, {0x180F8: 0xFF14})


if __name__ == "__main__":
    unittest.main()
