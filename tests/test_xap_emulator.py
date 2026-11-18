"""Tests for the XAP timing-engine emulator.

The firmware (vm.app) is git-ignored Sony material, so these run only where it is
present locally; they are skipped otherwise. Run: uv run python -m unittest discover -s tests
"""
import struct
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import xap_emulator as xe  # noqa: E402

# vm.app lives in the main checkout's git-ignored work/ tree; search a few plausible spots.
CANDIDATES = [
    HERE.parent / "work" / "fw" / "p1" / "fs0" / "vm.app",
]
VMAPP = next((p for p in CANDIDATES if p.exists()), None)

DESCRIPTOR_BASE = 0x8E29  # data address of input 0's [pointer, count] descriptor


def oracle_schedule(words, input_no):
    """Read the timing table straight from memory, independently of the interpreter."""
    pointer = words[DESCRIPTOR_BASE + 2 * input_no + xe.DELTA]
    count = words[DESCRIPTOR_BASE + 1 + 2 * input_no + xe.DELTA]
    out = []
    for k in range(count):
        entry = (pointer + 3 * k) & 0xFFFF
        base = entry + xe.DELTA
        out.append((words[base] & 0x7FFF, words[base + 2]))
    return out


@unittest.skipUnless(VMAPP, "vm.app not available locally")
class Emulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = VMAPP.read_bytes()
        cls.words = list(struct.unpack(f">{len(data) // 2}H", data[:len(data) // 2 * 2]))

    def test_matches_independent_table_read_for_several_inputs(self):
        for input_no in (1, 2, 5, 6):
            self.assertEqual(xe.schedule_for_input(self.words, input_no),
                             oracle_schedule(self.words, input_no), input_no)

    def test_shipped_power_button_pairs_at_7_seconds(self):
        schedule = dict((event, delay) for delay, event in xe.schedule_for_input(self.words, 1))
        self.assertEqual(schedule[0x15], 7000)  # 0x15 = enter-pairing long hold
        self.assertEqual(schedule[0x13], 1500)  # 0x13 = short hold

    def test_patch_changes_only_the_pairing_hold(self):
        patched = xe.apply_pairing_patch(self.words, 4000)
        schedule = dict((event, delay) for delay, event in xe.schedule_for_input(patched, 1))
        self.assertEqual(schedule[0x15], 4000)
        self.assertEqual(schedule[0x16], 4000)
        self.assertEqual(schedule[0x13], 1500)  # short hold untouched
        self.assertEqual(schedule[0x14], 1500)

    def test_patch_touches_only_uncovered_words(self):
        # The relocated table and the descriptor are the only edits, and both must be
        # words the signature does not cover (same property the flashed patch relies on).
        patched = xe.apply_pairing_patch(self.words, 4000)
        changed = [i for i, (a, b) in enumerate(zip(self.words, patched)) if a != b]
        self.assertEqual(set(changed),
                         {xe.PAIRING_DESCRIPTOR, *range(xe.PAIRING_NEW_TABLE, xe.PAIRING_NEW_TABLE + 12)})

    def test_five_second_hold_reaches_pairing_only_after_patch(self):
        shipped = xe.schedule_for_input(self.words, 1)
        patched = xe.schedule_for_input(xe.apply_pairing_patch(self.words, 4000), 1)
        self.assertNotIn(0x15, [e for d, e in shipped if d <= 5000])
        self.assertIn(0x15, [e for d, e in patched if d <= 5000])

    def test_interpreter_rejects_unmodelled_opcodes(self):
        # A guard: feed a word the cut-down core does not implement and expect a loud failure,
        # not a silent wrong answer.
        cpu = xe.Xap([0x0000] * 0x40000)
        cpu.W[0x1000] = 0x00aa  # opcode 0xa (shift): not on the timing path, so not modelled
        with self.assertRaises(xe.Unmodelled):
            cpu.run(0x1000, max_steps=10)


if __name__ == "__main__":
    unittest.main()
