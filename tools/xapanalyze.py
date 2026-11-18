#!/usr/bin/env python3
"""Static analysis helpers for vm.app (XAP bytecode), on top of xapdis.py.

    uv run tools/xapanalyze.py vm.app traps            # firmware calls and their callers
    uv run tools/xapanalyze.py vm.app funcs 0x31000 0x34000
    uv run tools/xapanalyze.py vm.app dump 0x33280     # annotated listing of one function
    uv run tools/xapanalyze.py vm.app callers 0x32f3f
    uv run tools/xapanalyze.py vm.app strings

Addresses are 16-bit word offsets. Findings are in docs/firmware-analysis.md.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xapdis import disasm  # noqa: E402

TRAP_ENTRY = 0x286  # stubs load the call number into X and jump here
STUB_PROLOGUE = "sys 0xfd0b"
CODE_END = 220400  # first word past the code in firmware 4.5.2
VOLUME_TABLE_STRING_WORD = 221090  # "volume_table/mode_1.bin", used to calibrate data addresses


def parse_int(text: str) -> int:
    return int(text, 0)


class VmApp:
    def __init__(self, path: Path):
        data = path.read_bytes()
        self.words = struct.unpack(f">{len(data) // 2}H", data[:len(data) // 2 * 2])
        self.ins = disasm(self.words[:CODE_END])
        self.index = {x[0]: i for i, x in enumerate(self.ins)}
        self.delta = self._find_data_delta()
        self.stubs = self._find_stubs()
        self.callers: dict[int, list[int]] = collections.defaultdict(list)
        for x in self.ins:
            target = self.call_target(x)
            if target is not None:
                self.callers[target].append(x[0])
        self.starts = sorted(a for a in self.callers if a in self.index)

    def _find_data_delta(self) -> int:
        """Six consecutive 'volume_table/mode_N.bin' pointers, each string 24 words apart."""
        for i in range(len(self.words) - 6):
            if all(self.words[i + k + 1] - self.words[i + k] == 24 for k in range(5)):
                return VOLUME_TABLE_STRING_WORD - self.words[i]
        raise SystemExit("could not calibrate data addresses: unexpected firmware layout")

    @staticmethod
    def call_target(x) -> int | None:
        match = re.match(r"bsr rel (-?0x[0-9a-f]+)", x[2])
        return x[0] + int(match[1], 16) if match else None

    def _find_stubs(self) -> dict[int, int]:
        stubs = {}
        for i, x in enumerate(self.ins[:-5]):
            if x[2] != STUB_PROLOGUE:
                continue
            number = None
            for y in self.ins[i + 1:i + 6]:
                if y[2].startswith("ld X, #"):
                    number = y[3]
                if re.fullmatch(rf"bra {TRAP_ENTRY:#x}", y[2]) and number is not None:
                    stubs[x[0]] = number & 0xFFFF
                    break
        return stubs

    def string_at(self, value: int) -> str | None:
        address = (value & 0xFFFF) + self.delta
        if not 0 < address < len(self.words) or 32 <= self.words[address - 1] < 127:
            return None
        text = ""
        while address < len(self.words) and 32 <= self.words[address] < 127 and len(text) < 60:
            text += chr(self.words[address])
            address += 1
        return text if len(text) >= 3 else None

    def annotate(self, x) -> str:
        target = self.call_target(x)
        if target is not None:
            return f"TRAP {self.stubs[target]}" if target in self.stubs else f"call {target:06x}"
        if x[3] is not None and (x[3] & 0xFFFF) >= 0x4000:
            text = self.string_at(x[3])
            if text:
                return f'{x[2]}  ; "{text}"'
        return x[2]

    def function_start(self, address: int) -> int:
        i = bisect.bisect_right(self.starts, address) - 1
        return self.starts[i] if i >= 0 else 0

    def function_end(self, address: int) -> int:
        i = bisect.bisect_right(self.starts, address)
        return self.starts[i] if i < len(self.starts) else CODE_END

    def body(self, start: int, end: int | None = None):
        stop = self.index.get(end or self.function_end(start), len(self.ins))
        return self.ins[self.index[start]:stop]


def command_traps(app: VmApp, args) -> None:
    sites = collections.defaultdict(list)
    for stub, number in app.stubs.items():
        for caller in app.callers[stub]:
            sites[number].append(caller)
    for number in sorted(sites):
        functions = sorted({app.function_start(c) for c in sites[number]})
        shown = " ".join(f"{f:06x}" for f in functions[:6])
        print(f"{number:4d} calls={len(sites[number]):4d} functions={len(functions):3d}  {shown}")


def command_funcs(app: VmApp, args) -> None:
    for start in (s for s in app.starts if args.start <= s < args.end):
        traps, values = collections.Counter(), collections.Counter()
        for x in app.body(start):
            target = app.call_target(x)
            if target in app.stubs:
                traps[app.stubs[target]] += 1
            elif x[3] is not None and x[2].split()[0] in ("ld", "cmp") and not -2 <= x[3] <= 8:
                values[x[3] & 0xFFFF] += 1
        common = " ".join(hex(k) for k, _ in values.most_common(8))
        print(f"{start:06x} len={app.function_end(start) - start:4d} callers={len(app.callers[start]):3d} "
              f"traps={dict(traps)} values=[{common}]")


def command_dump(app: VmApp, args) -> None:
    for x in app.body(args.address, args.end):
        if not x[2].startswith("prefix"):
            print(f"{x[0]:06x} {app.annotate(x)}")


def command_callers(app: VmApp, args) -> None:
    for caller in app.callers.get(args.address, []):
        print(f"{caller:06x} in {app.function_start(caller):06x}")


def command_strings(app: VmApp, args) -> None:
    run: list[int] = []
    for i, value in enumerate(app.words):
        if 32 <= value < 127:
            run.append(value)
            continue
        if len(run) >= 4:
            print(f"{i - len(run):06x} {''.join(map(chr, run))}")
        run = []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vmapp", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("traps").set_defaults(run=command_traps)
    funcs = sub.add_parser("funcs")
    funcs.add_argument("start", type=parse_int)
    funcs.add_argument("end", type=parse_int)
    funcs.set_defaults(run=command_funcs)
    dump = sub.add_parser("dump")
    dump.add_argument("address", type=parse_int)
    dump.add_argument("end", type=parse_int, nargs="?")
    dump.set_defaults(run=command_dump)
    callers = sub.add_parser("callers")
    callers.add_argument("address", type=parse_int)
    callers.set_defaults(run=command_callers)
    sub.add_parser("strings").set_defaults(run=command_strings)
    args = parser.parse_args()
    args.run(VmApp(args.vmapp), args)


if __name__ == "__main__":
    main()
