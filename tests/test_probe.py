"""Tests for the assembler and the key-read probe, on the modelled machine.

These need no firmware: the probe runs against a synthetic persistent store, so they
exercise the interpreter extensions (PsRetrieve/LED traps) and the assembler directly.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import xap_asm as a  # noqa: E402
import xap_emulator as xe  # noqa: E402
from xapdis import disasm  # noqa: E402
from probe_demo import build_probe, run_probe  # noqa: E402

KNOWN0, KNOWN1 = 0x92F5, 0xFE19  # arbitrary stand-ins for the key's first two words


class Assembler(unittest.TestCase):
    def test_immediate_encoding_round_trips(self):
        for v in (0, 1, -1, 127, 128, 255, 350, 0x7FFF, -0x80, 0x92F5, 0xFE19, 0xC600):
            prefixes, opd = a.enc_imm(v)
            # rebuild exactly as the decoder does
            val = a.sx8(opd)
            if prefixes:
                val += a.sx8(prefixes[-1]) << 8
            if len(prefixes) == 2:
                val += a.sx8(prefixes[0]) << 16
            self.assertEqual(val, v, hex(v))

    def test_assembles_to_decodable_instructions(self):
        words, labels = a.asm([("enter",), ("ld", "AL", "#", 350), ("leave",)], base=0x200)
        texts = [t for _, _, t, _ in disasm(words, 0x200)]
        self.assertIn("ld AL, #0x15e", texts)


class Probe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = build_probe(KNOWN0, KNOWN1)

    def test_match_gives_one_blink(self):
        self.assertEqual(run_probe(self.probe, {350: [KNOWN0, KNOWN1, 0x1234]}), 1)

    def test_different_gives_two_blinks(self):
        self.assertEqual(run_probe(self.probe, {350: [KNOWN0 ^ 0xFFFF, KNOWN1]}), 2)
        self.assertEqual(run_probe(self.probe, {350: [KNOWN0, KNOWN1 ^ 0x1]}), 2)

    def test_absent_gives_three_blinks(self):
        self.assertEqual(run_probe(self.probe, {}), 3)

    def test_probe_exceeds_one_uncovered_slot(self):
        # Honest check: the probe is bigger than a single 15-word uncovered window,
        # so a real deployment must split it across fragments (as discussed).
        self.assertGreater(len(self.probe), 15)


if __name__ == "__main__":
    unittest.main()
