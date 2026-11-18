#!/usr/bin/env python3
"""Link the read-probe into the REAL firmware's free space and show the signature survives.

This places the probe's fragments into actual uncovered padding words of vm.app, then runs
the real signature/checksum verifier (tools/dfu_sign.py). It proves the injected code lands
only where Sony's signature does not reach, so the signature still validates -- the same
property the pairing patch relied on. It writes nothing to any device, and installs no hook
(placing a hook on a real power-on call is the next step).

    uv run tools/inject_demo.py <path-to-firmware.bin>
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import xap_inject as inj  # noqa: E402
import dfu_sign as d  # noqa: E402
from probe_demo import probe_program  # noqa: E402


def main() -> int:
    img = Path(sys.argv[1]).read_bytes()
    s = d.parse_sections(img)
    fs = d.words_le(img[s.fs[0]:s.fs[0] + s.fs[1]])
    L = d.VmLayout(fs)
    vm = fs[L.start:L.end]
    key350 = d.read_ps_keys(img, s.sps)[350]

    slots = inj.free_slots(vm, L.covered)
    print(f"free slots (uncovered padding): {len(slots)}, "
          f"{sum(x.length for x in slots)} words total; longest {max(x.length for x in slots)}")

    edits, _, entry = inj.Linker(slots).link(probe_program(key350[0], key350[1]), covered=L.covered)
    used = sorted({a for a in edits})
    print(f"probe linked into {len({a // 16 for a in used})}+ fragments, entry at vm word {entry:#x}")
    print(f"{len(edits)} word edits, all uncovered: {not any(L.covered(a) for a in edits)}")

    patched = d.patch(img, edits)                 # applies edits and repairs checksums/CRC
    results = d.verify(patched)
    print("\nsignature / checksum verification of the patched firmware:")
    for name, ok in results.items():
        print(f"  {name:16} {'OK' if ok else 'FAILED'}")
    print("\nAll pass -> the probe fits in free space with Sony's signature intact."
          if all(results.values()) else "\nverification FAILED")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
