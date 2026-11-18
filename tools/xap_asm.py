#!/usr/bin/env python3
"""A tiny XAP assembler, the inverse of xapdis.py, for writing probe/patch snippets.

Only the instruction forms the emulator executes are supported. Immediates are
encoded with prefix words exactly as the decoder reconstructs them. Branch and
trap-call offsets must fit in a signed byte (keep targets near the code); this is
checked, not assumed.

A program is a list of tuples; `asm(program, base)` returns (words, labels).

    ("enter",) / ("leave",)
    ("ld"|"st"|"cmp"|"add"|"and", reg, mode, value)   reg in AH/AL/X/Y
                                                       mode "#" imm, "X"/"Y" indexed
    ("bra"|"beq"|"bne", label)
    ("trap", n)                 # emitted as bsr to a per-trap virtual stub (see emulator)
    ("label", name)             # marks a position
"""
from __future__ import annotations

REGNO = {"AH": 0, "AL": 1, "X": 2, "Y": 3}
OPC = {"ld": 1, "st": 2, "add": 3, "cmp": 8, "and": 0xC}
MODE = {"#": 0, "abs": 1, "X": 2, "Y": 3}
BR = {"bra": (0xE, 0), "bne": (0xF, 0), "beq": (0xF, 1)}
TRAP_STUB_BASE = 0x1F0  # virtual stubs live just below the usual probe base 0x200


def sx8(v: int) -> int:
    return v - 0x100 if v & 0x80 else v


def enc_imm(v: int) -> tuple[list[int], int]:
    """Prefix bytes (high first) and the opcode's opd byte so the decoder rebuilds v."""
    for npre in (0, 1, 2):
        opd = v & 0xFF
        rem = v - sx8(opd)
        if npre == 0:
            if rem == 0:
                return [], opd
            continue
        p1 = (rem >> 8) & 0xFF
        rem -= sx8(p1) << 8
        if npre == 1:
            if rem == 0:
                return [p1], opd
            continue
        p2 = (rem >> 16) & 0xFF
        if rem - (sx8(p2) << 16) == 0:
            return [p2, p1], opd
    raise ValueError(f"cannot encode immediate {v:#x}")


def _size(instr) -> int:
    op = instr[0]
    if op in ("enter", "leave", "label"):
        return 0 if op == "label" else 1
    if op in BR or op == "trap":
        return 1  # near target; verified in pass 2
    _, _, mode, value = instr
    val = value if mode == "#" else value  # offsets are small; same path
    return len(enc_imm(val)[0]) + 1


def trap_targets(base: int = TRAP_STUB_BASE) -> dict[int, int]:
    """Map the virtual stub address the assembler calls -> trap number, for the emulator."""
    return {base + 2 * i: n for i, n in enumerate(TRAP_NUMS)}


TRAP_NUMS = [9, 24, 23, 171]  # alloc, PsRetrieve, PsStore, LED
_TRAP_ADDR = {n: TRAP_STUB_BASE + 2 * i for i, n in enumerate(TRAP_NUMS)}


def asm(program, base: int = 0x200) -> tuple[list[int], dict[str, int]]:
    # pass 1: addresses
    addr = base
    labels: dict[str, int] = {}
    for instr in program:
        if instr[0] == "label":
            labels[instr[1]] = addr
        addr += _size(instr)
    # pass 2: encode
    words: list[int] = []
    addr = base
    for instr in program:
        op = instr[0]
        if op == "label":
            continue
        start = addr
        if op == "enter":
            words.append(0xFC0B)
        elif op == "leave":
            words.append(0xFC0F)
        elif op in BR:
            opc, reg = BR[op]
            rel = labels[instr[1]] - start
            if not -0x80 <= rel <= 0x7F:
                raise ValueError(f"{op} target too far ({rel}); keep branches local")
            words.append(((rel & 0xFF) << 8) | (opc << 4) | (reg << 2))
        elif op == "trap":
            rel = _TRAP_ADDR[instr[1]] - start
            if not -0x80 <= rel <= 0x7F:
                raise ValueError(f"trap stub too far ({rel}); move TRAP_STUB_BASE nearer base")
            words.append(((rel & 0xFF) << 8) | (9 << 4) | (3 << 2))  # bsr rel
        else:
            _, reg, mode, value = instr
            prefixes, opd = enc_imm(value)
            for p in prefixes:
                words.append(p << 8)
            words.append((opd << 8) | (OPC[op] << 4) | (REGNO[reg] << 2) | MODE[mode])
        addr += _size(instr)
    return words, labels


if __name__ == "__main__":
    # round-trip self-test against the disassembler
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from xapdis import disasm

    prog = [
        ("enter",),
        ("ld", "AL", "#", 350),
        ("ld", "AH", "#", 0x92F5),
        ("cmp", "AL", "#", 0),
        ("beq", "end"),
        ("add", "AL", "#", -1),
        ("st", "AL", "Y", 2),
        ("label", "end"),
        ("leave",),
    ]
    words, labels = asm(prog, base=0x200)
    for addr, w, text, imm in disasm(words, 0x200):
        print(f"{addr:#06x}: {w:04x}  {text}")
    print("labels:", {k: hex(v) for k, v in labels.items()})
