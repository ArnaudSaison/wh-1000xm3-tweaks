#!/usr/bin/env python3
"""Emulate a 'can vm.app read the verification key?' probe, offline.

The probe (written in tools/xap_asm.py) reads persistent-store key 350, compares its
first two words against the public modulus we already know, and blinks the LED:

    1 blink  = read returned our known key   (app context CAN read it)
    2 blinks = read returned something else   (reachable, different)
    3 blinks = read returned nothing          (walled off)

This runs the probe on the modelled machine against all three outcomes to show the
logic is correct. It does NOT tell us which outcome is real -- that is the behaviour
of the hardware's PsRetrieve trap, which is modelled here, and only a real device
(or SPI) can answer. Nothing is written to any headset.

    uv run tools/probe_demo.py <path-to-firmware.bin>
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import xap_asm as a  # noqa: E402
import xap_emulator as xe  # noqa: E402

PROBE_BASE = 0x200


def probe_program(known0: int, known1: int) -> list:
    """The read-compare-blink probe as an assembler program (reusable by the linker)."""
    return [
        ("enter",),
        ("ld", "AL", "#", 16), ("trap", 9), ("st", "AH", "Y", 0),      # buf = alloc(16); local0 = buf
        ("ld", "AL", "#", 350), ("ld", "AH", "Y", 0), ("trap", 24),    # PsRetrieve(350) -> AL=len, buf filled
        ("cmp", "AL", "#", 0), ("beq", "three"),                       # nothing -> 3 blinks
        ("ld", "X", "Y", 0), ("ld", "AL", "X", 0), ("cmp", "AL", "#", known0), ("bne", "two"),
        ("ld", "AL", "X", 1), ("cmp", "AL", "#", known1), ("bne", "two"),
        ("ld", "AL", "#", 1), ("bra", "blink"),                        # match -> 1 blink
        ("label", "two"), ("ld", "AL", "#", 2), ("bra", "blink"),      # different -> 2
        ("label", "three"), ("ld", "AL", "#", 3),                      # empty -> 3
        ("label", "blink"), ("st", "AL", "Y", 1),                      # local1 = blink count
        ("label", "loop"),
        ("ld", "AL", "Y", 1), ("cmp", "AL", "#", 0), ("beq", "done"),
        ("ld", "AL", "#", 1), ("trap", 171),                           # LED on
        ("ld", "AL", "#", 0), ("trap", 171),                           # LED off
        ("ld", "AL", "Y", 1), ("add", "AL", "#", -1), ("st", "AL", "Y", 1),
        ("bra", "loop"),
        ("label", "done"), ("leave",),
    ]


def build_probe(known0: int, known1: int) -> list[int]:
    words, _ = a.asm(probe_program(known0, known1), base=PROBE_BASE)
    return words


def run_probe(probe_words: list[int], ps: dict[int, list[int]]) -> int:
    mem = [0] * 0x300
    for i, w in enumerate(probe_words):
        mem[PROBE_BASE + i] = w
    cpu = xe.Xap(mem)
    cpu.trap_targets = a.trap_targets()
    cpu.ps = ps
    cpu.run(PROBE_BASE, max_steps=10000)
    return cpu.led.count(1)  # number of LED-on pulses = blink count


def main() -> int:
    import dfu_sign as d
    img = Path(sys.argv[1]).read_bytes()
    sections = d.parse_sections(img)
    key350 = d.read_ps_keys(img, sections.sps)[350]
    known0, known1 = key350[0], key350[1]
    print(f"known key 350 starts: {known0:#06x} {known1:#06x}  (we already have this; nothing to extract)")

    probe = build_probe(known0, known1)
    print(f"probe assembles to {len(probe)} words at {PROBE_BASE:#x}\n")

    cases = {
        "real key present":      {350: key350},
        "reachable but different": {350: [known0 ^ 0xFFFF] + key350[1:]},
        "walled off (no key)":   {},
    }
    expected = {"real key present": 1, "reachable but different": 2, "walled off (no key)": 3}
    ok = True
    for name, ps in cases.items():
        blinks = run_probe(probe, dict(ps))
        mark = "ok" if blinks == expected[name] else "MISMATCH"
        ok = ok and blinks == expected[name]
        print(f"  {name:26}: {blinks} blink(s)   [{mark}]")
    print("\nAll three cases produced the intended blink count." if ok else "\nLOGIC ERROR.")
    print("Which case is real on your headset is a hardware fact this cannot predict.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
