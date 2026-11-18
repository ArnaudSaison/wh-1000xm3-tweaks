"""Prove the injector: automatic fragment-splitting preserves behaviour, and the
trampoline hook runs injected code while preserving the call it replaces. No firmware,
no device -- everything runs on the modelled machine."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import xap_asm as a  # noqa: E402
import xap_inject as inj  # noqa: E402
import xap_emulator as xe  # noqa: E402
from probe_demo import probe_program  # noqa: E402

KNOWN0, KNOWN1 = 0x92F5, 0xFE19


def load(mem_size, edits):
    mem = [0] * mem_size
    for addr, word in edits.items():
        mem[addr] = word
    return mem


class Splitting(unittest.TestCase):
    def setUp(self):
        # deliberately tiny, scattered slots so the probe must break into many fragments
        self.slots = [inj.Slot(0x400 + 0x10 * i, 7) for i in range(60)]

    def run_probe(self, ps):
        edits, _, entry = inj.Linker(self.slots).link(probe_program(KNOWN0, KNOWN1))
        cpu = xe.Xap(load(0x800, edits))
        cpu.trap_targets = a.trap_targets()
        cpu.ps = ps
        cpu.run(entry, max_steps=20000)
        return cpu.led.count(1), edits

    def test_split_probe_behaves_exactly_like_the_contiguous_one(self):
        self.assertEqual(self.run_probe({350: [KNOWN0, KNOWN1, 0]})[0], 1)
        self.assertEqual(self.run_probe({350: [KNOWN0 ^ 0xFFFF, KNOWN1]})[0], 2)
        self.assertEqual(self.run_probe({})[0], 3)

    def test_it_really_fragmented(self):
        _, edits = self.run_probe({})
        used = {(addr - 0x400) // 0x10 for addr in edits}
        self.assertGreaterEqual(len(used), 8)  # scattered across many slots, not one blob

    def test_link_refuses_to_touch_covered_words(self):
        with self.assertRaisesRegex(ValueError, "covered"):
            inj.Linker(self.slots).link(probe_program(KNOWN0, KNOWN1), covered=lambda i: True)


class Trampoline(unittest.TestCase):
    def build(self):
        link = inj.Linker
        orig_edits, orig_labels, orig = link([inj.Slot(0x440, 0x20)]).link(
            [("label", "orig"), ("ld", "AL", "#", 9), ("trap", 171), ("ret",)])
        inj_edits, _, inj_entry = link([inj.Slot(0x460, 0x40)]).link(
            inj.trampoline([("ld", "AL", "#", 1), ("trap", 171)], original_target=orig))
        host_edits, host_labels, host = link([inj.Slot(0x400, 0x20)]).link(
            [("enter",), ("ld", "AL", "#", 5), ("trap", 171),
             ("label", "hook"), ("call_abs", orig),
             ("ld", "AL", "#", 7), ("trap", 171), ("leave",)])
        mem = load(0x1000, {**orig_edits, **inj_edits, **host_edits})
        return mem, host, host_labels["hook"], inj_entry

    def led(self, mem, entry):
        cpu = xe.Xap(mem)
        cpu.trap_targets = a.trap_targets()
        cpu.run(entry, max_steps=10000)
        return cpu.led

    def test_without_hook_runs_only_the_original(self):
        mem, host, _, _ = self.build()
        self.assertEqual(self.led(mem, host), [5, 9, 7])

    def test_hook_inserts_injected_code_and_keeps_the_original(self):
        mem, host, hook_addr, inj_entry = self.build()
        for addr, word in inj.repoint_call(mem, hook_addr, inj_entry).items():
            mem[addr] = word
        # 5 = host before, 1 = injected, 9 = original call still happens, 7 = host after
        self.assertEqual(self.led(mem, host), [5, 1, 9, 7])

    def test_repoint_rejects_size_mismatch(self):
        mem, host, hook_addr, _ = self.build()
        with self.assertRaisesRegex(ValueError, "same-length|needs"):
            inj.repoint_call(mem, hook_addr, 0x90000)  # far target needs more words than the call has


if __name__ == "__main__":
    unittest.main()
