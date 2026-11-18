#!/usr/bin/env python3
"""A tiny XAP interpreter, just enough to run the WH-1000XM3 button-timing engine.

    uv run tools/xap_emulator.py path/to/vm.app        # emulate input 1, show the schedule
    uv run tools/xap_emulator.py path/to/vm.app --hold 5000

This is NOT a full chip emulator. It executes the real bytecode of two functions
in vm.app on a modelled machine:

  * 0x11523  the per-input dispatch: reads the timing descriptor for an input
             number and walks its table, calling the leaf once per entry.
  * 0x114FD  the leaf: for one entry, posts a delayed message whose delay is the
             entry's duration (firmware call 16).

Every firmware call (trap) is stubbed: alloc (9) hands back scratch RAM, and
post-message (16) records (delay, event) instead of sending anything. So running
the dispatch for an input yields the list of (delay_ms, event) the firmware would
schedule when that button is first pressed. That is exactly the data the pairing
patch changes, so the same patch can be applied in memory and re-emulated.

Only the opcodes those two functions use are implemented; anything else raises,
so the emulator fails loudly rather than guessing. Encoding follows xapdis.py.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

DELTA = 0x2E943           # data address D is held in file word D + DELTA
RAM_BASE = 0xC600         # data addresses at/above this are scratch RAM, not file-backed
FRAME_BASE = 0xC600       # Y-frames grow upward from here
FRAME_SIZE = 0x40
HEAP_BASE = 0xF000        # trap-9 allocations
SFR = {0xFFFFE0: "AL", 0xFFFFE1: "AH"}  # the register mirrors this path reads
REG = ["AH", "AL", "X", "Y"]
STUB_LO, STUB_HI = 0x40, 0x286          # trap stubs live here; the trap entry is 0x286

DISPATCH = 0x11523        # entry of the per-input dispatch function
TRAP_ALLOC = 9
TRAP_POST = 16
TRAP_PSRETRIEVE = 24      # read a persistent-store key (modelled)
TRAP_PSSTORE = 23         # write a persistent-store key (modelled)
TRAP_LED = 171            # LED control candidate (modelled)

EVENT_NAMES = {
    0x13: "power on / short-hold action",
    0x14: "short-hold release",
    0x15: "ENTER PAIRING (long hold)",
    0x16: "long-hold release",
}


def sx8(v: int) -> int:
    return v - 0x100 if v & 0x80 else v


class Unmodelled(Exception):
    """Raised when the bytecode uses something this cut-down interpreter does not model."""


class Xap:
    def __init__(self, words: list[int]):
        self.W = words
        self.ram: dict[int, int] = {}
        self.r = {"AH": 0, "AL": 0, "X": 0, "Y": 0}
        self.Z = False
        self.N = False
        self.C = False
        self.callstack: list[int | None] = []
        self.framestack: list[int] = []
        self.frame_next = FRAME_BASE
        self.heap_next = HEAP_BASE
        self.schedule: list[tuple[int, int, int]] = []  # (delay_ms, event, entry_ptr)
        self.steps = 0
        self.trap_targets: dict[int, int] = {}  # extra virtual stubs: address -> trap number
        self.ps: dict[int, list[int]] = {}       # modelled persistent store (key -> words)
        self.led: list[int] = []                 # modelled LED trap: each call's value

    # --- memory -------------------------------------------------------------
    def rd(self, addr: int) -> int:
        addr &= 0xFFFFFF
        if addr in SFR:
            return self.r[SFR[addr]]
        if RAM_BASE <= addr <= 0xFFFF:
            return self.ram.get(addr, 0)
        fw = (addr & 0xFFFF) + DELTA
        if 0 <= fw < len(self.W):
            return self.W[fw]
        return self.ram.get(addr & 0xFFFF, 0)

    def wr(self, addr: int, val: int) -> None:
        addr &= 0xFFFFFF
        val &= 0xFFFF
        if addr in SFR:
            self.r[SFR[addr]] = val
            return
        if RAM_BASE <= addr <= 0xFFFF:
            self.ram[addr] = val
            return
        raise Unmodelled(f"store to non-RAM address {addr:#x} (would overwrite firmware)")

    # --- traps --------------------------------------------------------------
    def _trap_number(self, target: int) -> int:
        """A stub loads its trap number into X then jumps to 0x286. Read that number."""
        pre = npre = 0
        for a in range(target, target + 8):
            w = self.W[a]
            if (w & 0xFF) == 0 and w != 0:  # prefix word
                pre = (pre << 8 | (w >> 8)) if npre else (w >> 8)
                npre += 1
                continue
            mode, reg, opc, opd = w & 3, (w >> 2) & 3, (w >> 4) & 15, w >> 8
            if opc == 1 and reg == 2 and mode == 0:  # ld X, #n
                val = sx8(opd) + ((sx8(pre & 0xFF) << 8) if npre else 0)
                return val & 0xFFFF
            pre = npre = 0
        raise Unmodelled(f"no trap number in stub at {target:#x}")

    def _do_trap(self, num: int) -> None:
        if num == TRAP_ALLOC:
            size = max(self.r["AL"] & 0xFFFF, 1)
            ptr = self.heap_next
            self.heap_next += size
            self.r["AH"] = ptr          # the caller reads the result from AH (0xFFFFE1)
        elif num == TRAP_PSRETRIEVE:    # model: AL=key, AH=buffer; fills buffer, returns length in AL
            words = self.ps.get(self.r["AL"] & 0xFFFF, [])
            buf = self.r["AH"]
            for i, wv in enumerate(words[:16]):
                self.wr((buf + i) & 0xFFFF, wv)
            self.r["AL"] = len(words)
        elif num == TRAP_PSSTORE:       # model: AL=key, AH=buffer, X=count; writes the store
            count = self.r["X"] & 0xFFFF
            buf = self.r["AH"]
            self.ps[self.r["AL"] & 0xFFFF] = [self.rd((buf + i) & 0xFFFF) for i in range(count)]
            self.r["AL"] = count
        elif num == TRAP_LED:           # model: AL = LED value; record each call
            self.led.append(self.r["AL"] & 0xFFFF)
        elif num == TRAP_POST:
            y = self.r["Y"]
            delay = self.rd(y + 2)       # local2 = duration
            buffer = self.rd(y + 0)      # local0 = message buffer
            entry_ptr = self.rd(buffer)  # buffer[0] = the table-entry pointer
            event = self.rd((entry_ptr + 2) & 0xFFFF)
            self.schedule.append((delay, event, entry_ptr & 0xFFFF))
        else:
            raise Unmodelled(f"trap {num} is not modelled")

    # --- execution ----------------------------------------------------------
    def run(self, pc: int, max_steps: int = 200000, trace: bool = False) -> None:
        self.callstack = [None]  # the final leave returns here and halts
        self.framestack = []
        while True:
            self.steps += 1
            if self.steps > max_steps:
                raise Unmodelled("step limit exceeded (runaway execution)")
            pre = npre = 0
            while (self.W[pc] & 0xFF) == 0 and self.W[pc] != 0:  # consume prefix words
                opd = self.W[pc] >> 8
                pre = (pre << 8 | opd) if npre else opd
                npre += 1
                pc += 1
            ia = pc
            w = self.W[ia]
            if trace:
                print(f"  s{self.steps:3d} ia={ia:#07x} w={w:#06x} "
                      f"AH={self.r['AH']:#06x} AL={self.r['AL']:#06x} X={self.r['X']:#06x} Y={self.r['Y']:#06x}")
            mode, reg, opc, opd = w & 3, (w >> 2) & 3, (w >> 4) & 15, w >> 8
            val = sx8(opd) + ((sx8(pre & 0xFF) << 8) if npre else 0) + \
                  ((sx8((pre >> 8) & 0xFF) << 16) if npre == 2 else 0)
            nxt = ia + 1

            if w == 0x00E2:  # rts
                ret = self.callstack.pop()
                if ret is None:
                    return
                pc = ret
                continue

            if opc == 0:  # sys: frame enter (reg 2) / leave+return (reg 3)
                if reg == 2:
                    self.framestack.append(self.r["Y"])
                    self.r["Y"] = self.frame_next
                    self.frame_next += FRAME_SIZE
                elif reg == 3:
                    self.frame_next -= FRAME_SIZE
                    self.r["Y"] = self.framestack.pop()
                    ret = self.callstack.pop()
                    if ret is None:
                        return
                    pc = ret
                    continue
                else:
                    raise Unmodelled(f"sys {w:#06x} at {ia:#x}")
                pc = nxt
                continue

            if opc == 9:  # smult / sdiv / tst / bsr
                if reg == 3:  # bsr (call)
                    target = ia + val  # code address: full width, not masked to 16 bits
                    if target in self.trap_targets:        # virtual stub (assembled snippets)
                        self._do_trap(self.trap_targets[target])
                        pc = nxt
                        continue
                    if STUB_LO <= target < STUB_HI:
                        self._do_trap(self._trap_number(target))
                        pc = nxt
                        continue
                    self.callstack.append(nxt)
                    pc = target
                    continue
                if reg == 2:  # tst
                    v = self.rd((val + self.r["Y"]) & 0xFFFF) if mode == 3 else \
                        self.rd((val + self.r["X"]) & 0xFFFF) if mode == 2 else \
                        self.rd(val) if mode == 1 else val
                    self.Z = (v & 0xFFFF) == 0
                    self.N = bool(v & 0x8000)
                    pc = nxt
                    continue
                raise Unmodelled(f"opc 9 reg {reg} at {ia:#x}")

            if opc in (0xE, 0xF):  # conditional / unconditional branches
                name = (["bra", "blt", "bpl", "bmi"] if opc == 0xE else ["bne", "beq", "bcc", "bcs"])[reg]
                if mode != 0:
                    raise Unmodelled(f"branch mode {mode} at {ia:#x}")
                take = {"bra": True, "bne": not self.Z, "beq": self.Z,
                        "bcc": not self.C, "bcs": self.C, "bmi": self.N, "bpl": not self.N}.get(name)
                if take is None:
                    raise Unmodelled(f"branch {name} not modelled at {ia:#x}")
                pc = (ia + val) if take else nxt  # code address: full width
                continue

            if opc in (1, 2, 3, 8, 0xC):  # ld / st / add / cmp / and
                if mode == 0:
                    if opc == 2:          # store with immediate addressing: unused by this path
                        pc = nxt
                        continue
                    src, addr = val, None
                elif mode == 1:
                    addr = val & 0xFFFFFF
                    src = self.rd(addr)
                elif mode == 2:
                    addr = (val + self.r["X"]) & 0xFFFF
                    src = self.rd(addr)
                else:
                    addr = (val + self.r["Y"]) & 0xFFFF
                    src = self.rd(addr)

                rn = REG[reg]
                if opc == 2:              # st
                    self.wr(addr, self.r[rn])
                elif opc == 1:            # ld
                    self.r[rn] = src & 0xFFFF
                    self.Z = self.r[rn] == 0
                    self.N = bool(self.r[rn] & 0x8000)
                elif opc == 3:            # add
                    res = self.r[rn] + src
                    self.C = res > 0xFFFF
                    self.r[rn] = res & 0xFFFF
                    self.Z = self.r[rn] == 0
                    self.N = bool(self.r[rn] & 0x8000)
                elif opc == 8:            # cmp
                    res = (self.r[rn] - src) & 0xFFFF
                    self.Z = res == 0
                    self.N = bool(res & 0x8000)
                    self.C = self.r[rn] < (src & 0xFFFF)
                elif opc == 0xC:          # and
                    self.r[rn] = self.r[rn] & src & 0xFFFF
                    self.Z = self.r[rn] == 0
                    self.N = bool(self.r[rn] & 0x8000)
                pc = nxt
                continue

            raise Unmodelled(f"opcode {opc} (word {w:#06x}) at {ia:#x}")


def schedule_for_input(words: list[int], input_no: int) -> list[tuple[int, int]]:
    """Run the dispatch for one input number; return [(delay_ms, event), ...]."""
    cpu = Xap(words)
    cpu.r["AH"] = input_no & 0xFFFF
    cpu.r["AL"] = 0
    cpu.run(DISPATCH)
    return [(delay, event) for delay, event, _ in cpu.schedule]


# --- the pairing patch, expressed in vm.app word space ----------------------
PAIRING_DESCRIPTOR = 0x3776E   # input 1's table pointer (file word); uncovered by the signature
PAIRING_NEW_TABLE = 0x394E9    # relocated table, inside a debug string (uncovered)
PAIRING_NEW_TABLE_ADDR = (PAIRING_NEW_TABLE - DELTA) & 0xFFFF


def apply_pairing_patch(words: list[int], milliseconds: int) -> list[int]:
    """The same edit tools/dfu_sign.py 'pairing' makes, applied to vm.app words in memory."""
    out = list(words)
    table = [1500, 0, 0x13, 1500, 1, 0x14, milliseconds, 0, 0x15, milliseconds, 1, 0x16]
    for i, v in enumerate(table):
        out[PAIRING_NEW_TABLE + i] = v
    out[PAIRING_DESCRIPTOR] = PAIRING_NEW_TABLE_ADDR
    return out


def load_words(path: Path) -> list[int]:
    data = path.read_bytes()
    return list(struct.unpack(f">{len(data) // 2}H", data[:len(data) // 2 * 2]))


def _format(schedule: list[tuple[int, int]]) -> str:
    return "\n".join(f"    {delay:5d} ms  event {event:#04x}  {EVENT_NAMES.get(event, '?')}"
                     for delay, event in schedule)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vmapp", type=Path)
    ap.add_argument("--input", type=int, default=1, help="button input number (1 = power)")
    ap.add_argument("--ms", type=int, default=4000, help="patched pairing-hold time to demonstrate")
    ap.add_argument("--hold", type=int, help="also show which events a hold of this many ms reaches")
    args = ap.parse_args()

    words = load_words(args.vmapp)
    original = schedule_for_input(words, args.input)
    print(f"input {args.input}, as shipped (Sony 4.5.2):")
    print(_format(original))

    patched = schedule_for_input(apply_pairing_patch(words, args.ms), args.input)
    print(f"\ninput {args.input}, after the pairing patch (--ms {args.ms}):")
    print(_format(patched))

    if args.hold is not None:
        for label, sched in (("as shipped", original), ("patched", patched)):
            reached = sorted(e for d, e in sched if d <= args.hold)
            pairs = 0x15 in reached
            print(f"\nholding {args.hold} ms, {label}: "
                  + ("enters pairing" if pairs else "does NOT reach pairing")
                  + f" (events {[hex(e) for e in reached]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
