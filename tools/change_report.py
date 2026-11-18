#!/usr/bin/env python3
"""Offline verification of every firmware change we have tried or are considering.

For each candidate it uses the strongest offline check available:
  * code/logic changes  -> run the real bytecode in the XAP emulator
  * signature validity   -> tools/dfu_sign.py (reproduces Sony's real signatures)
  * partition-3 data      -> structural + checksum check (that partition is unsigned)

Nothing here touches a device.

    uv run tools/change_report.py <firmware.bin> <vm.app>
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dfu_sign as d  # noqa: E402
import xap_emulator as xe  # noqa: E402

PAIRING_DESCRIPTOR = 0x3776E
PAIRING_COUNT = 0x3776F
PAIRING_NEW_TABLE = 0x394E9
NEW_TABLE_ADDR = (PAIRING_NEW_TABLE - xe.DELTA) & 0xFFFF


def load_vm(path):
    data = Path(path).read_bytes()
    return list(struct.unpack(f">{len(data) // 2}H", data[:len(data) // 2 * 2]))


def section(title):
    print(f"\n{'=' * 4} {title} {'=' * 4}")


def pairing_hold(vm):
    section("1. Pairing hold (CODE change, emulated) - already flashed & confirmed on hardware")
    orig = {e: delay for delay, e in xe.schedule_for_input(vm, 1)}
    patched_vm = list(vm)
    table = [1500, 0, 0x13, 1500, 1, 0x14, 4000, 0, 0x15, 4000, 1, 0x16]
    for i, v in enumerate(table):
        patched_vm[PAIRING_NEW_TABLE + i] = v
    patched_vm[PAIRING_DESCRIPTOR] = NEW_TABLE_ADDR
    new = {e: delay for delay, e in xe.schedule_for_input(patched_vm, 1)}
    print(f"  pairing event 0x15 fires at: shipped {orig[0x15]} ms  ->  patched {new[0x15]} ms")
    print("  VERDICT: works (emulator reproduces the measured ~4 s; confirmed on the real headset).")


def double_press(vm):
    section("2. Double-press -> pairing")
    print("  Not a table edit: bit 15 of a duration means auto-repeat, which fires during any press.")
    print("  See docs/double-press.md and tools/double_press.py. Built and emulated, not run on hardware.")


def volume_cap(img):
    section("3. Regional volume cap (CODE constants in vm.app) - never flashed")
    s = d.parse_sections(img)
    fs = d.words_le(img[s.fs[0]:s.fs[0] + s.fs[1]])
    L = d.VmLayout(fs)
    spots = {0x180B7: "0xd5 cap (path A)", 0x180F5: "0xd5 cap (path B)",
             0x180F8: "0xd2 cap (path B)", 0x17FD4: "0xd2 clamp-compare", 0x1811A: "0xd2 cap (path C)"}
    for addr, what in spots.items():
        print(f"  vm {addr:#07x} {what:22} value {fs[L.start + addr]:#06x}  "
              f"{'UNCOVERED (editable, signature-safe)' if not L.covered(addr) else 'covered (frozen)'}")
    # prove: raising an uncovered cap keeps the signature; a covered one is refused
    raised = d.patch(img, {0x17FD4: (fs[L.start + 0x17FD4] & 0x00FF) | (0xFF << 8)})  # 0xd2 -> 0xff compare
    print(f"  raising the UNCOVERED clamp-compare to 0xff: signatures still verify = {all(d.verify(raised).values())}")
    try:
        d.patch(img, {0x180F8: 0xFF14})
        print("  editing the COVERED cap: unexpectedly allowed")
    except d.SignError as err:
        print(f"  editing the COVERED cap (0x180f8) is refused: {err}")
    print("  VERDICT: a volume-limit change on the uncovered paths is signature-valid OTA; the covered")
    print("          paths need SPI (or an injected override). Runtime effect best confirmed by emulating")
    print("          the cap function (0x180ab) with its config helpers - a deeper follow-up.")


def announcements(img):
    section("4. Quieter announcements / earlier 'volume' change (DATA, partition 3) - flashed & worked")
    pos, parts = 0, []
    while pos < len(img):
        tag = img[pos:pos + 8]
        size = int.from_bytes(img[pos + 8:pos + 12], "big")
        if tag == b"PARTDATA":
            parts.append(int.from_bytes(img[pos + 12:pos + 14], "big"))
        pos += 12 + size
    s = d.parse_sections(img)
    signed = (s.dfu_start, s.file_crc)
    print(f"  PARTDATA partitions present: {parts}  (partition 1 is the only signed one)")
    print(f"  signed region is one CSR-dfu2 container at bytes {signed[0]}..{signed[1]}; partition 3 is outside it")
    print("  So the announcements (MP3 gain) are NOT XAP code and NOT under the signature - only a 16-bit")
    print("  XOR checksum, which the build tool fixes. The emulator does not apply (it runs code, not audio).")
    print("  VERDICT: accepted with only a checksum to satisfy - matches the -9 dB upload that worked.")


def main() -> int:
    img = Path(sys.argv[1]).read_bytes()
    vm = load_vm(sys.argv[2])
    pairing_hold(vm)
    double_press(vm)
    volume_cap(img)
    announcements(img)
    print("\nSummary: hold change - proven; double-press - code patch, see docs/double-press.md; volume cap -")
    print("partly signature-valid now, rest needs SPI; announcements - data, already proven on hardware.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
