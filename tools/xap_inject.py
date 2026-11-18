#!/usr/bin/env python3
"""Reusable code injection for vm.app: split a snippet across uncovered slots and hook it in.

The signature bug leaves only <=15-word windows free, so injected logic must be scattered
across many small fragments. This module hides that:

  * `free_slots(...)`      - find uncovered padding runs usable as fragment slots.
  * `Linker(slots).link(program)` - lay an assembler program (see xap_asm) across the slots,
    inserting long jumps between fragments automatically, and resolve all labels. Returns
    {address: word} edits plus the entry address. Write any snippet as straight-line code
    with labels; you never place fragments by hand.
  * `trampoline(user, original_target)` - wrap a snippet so it saves/restores registers,
    runs, then tail-calls the function it replaced (for hooking an existing call).
  * `repoint_call(words, call_addr, new_target)` - redirect an existing `bsr` to the entry,
    in place and same-size (raises if the new target would not encode to the same length).

Everything here only computes word edits; it never talks to a device. Edits are meant to
land exclusively in signature-uncovered words so Sony's signature stays valid; `link`
checks that against a coverage predicate when one is supplied.
"""
from __future__ import annotations

from dataclasses import dataclass

from xap_asm import BR, MODE, OPC, REGNO, _TRAP_ADDR, enc_imm

RET = 0x00E2
ENTER = 0xFC0B
LEAVE = 0xFC0F
CJ_RESERVE = 3  # max words a between-fragment jump can take; kept free at each slot's tail


@dataclass
class Slot:
    addr: int
    length: int


def _encode_branch(base: int, target: int, opc: int, reg: int) -> list[int]:
    """`bra`/`bsr`-style: pick the fewest prefix words so decode gives target = ia + val."""
    for npre in (0, 1, 2):
        ia = base + npre
        prefixes, opd = enc_imm(target - ia)
        if len(prefixes) == npre:
            return [p << 8 for p in prefixes] + [(opd << 8) | (opc << 4) | (reg << 2)]
    raise ValueError(f"cannot encode branch {base:#x} -> {target:#x}")


def _encode(addr: int, instr, labels: dict) -> list[int]:
    op = instr[0]
    if op == "raw":
        return [instr[1]]
    if op == "enter":
        return [ENTER]
    if op == "leave":
        return [LEAVE]
    if op == "ret":
        return [RET]
    if op in BR:
        return _encode_branch(addr, labels.get(instr[1], addr), *BR[op])
    if op == "bra_abs":
        return _encode_branch(addr, instr[1], 0xE, 0)
    if op == "call_abs":
        return _encode_branch(addr, instr[1], 9, 3)
    if op == "trap":
        return _encode_branch(addr, _TRAP_ADDR[instr[1]], 9, 3)
    _, reg, mode, value = instr
    prefixes, opd = enc_imm(value)
    return [p << 8 for p in prefixes] + [(opd << 8) | (OPC[op] << 4) | (REGNO[reg] << 2) | MODE[mode]]


def free_slots(words, covered, min_len: int = CJ_RESERVE + 3, pad=(0x0000, 0xFFFF)) -> list[Slot]:
    """Maximal runs of uncovered AND padding words, long enough to be useful."""
    out, i, n = [], 0, len(words)
    while i < n:
        if not covered(i) and words[i] in pad:
            j = i
            while j < n and not covered(j) and words[j] in pad:
                j += 1
            if j - i >= min_len:
                out.append(Slot(i, j - i))
            i = j
        else:
            i += 1
    return out


class OutOfSpace(Exception):
    pass


class Linker:
    def __init__(self, slots: list[Slot]):
        self.slots = sorted(slots, key=lambda s: s.addr)

    def link(self, program, covered=None) -> tuple[dict[int, int], dict[str, int], int]:
        """Place `program` across the slots. Returns (edits, labels, entry_address)."""
        labels: dict[str, int] = {}
        plan: list = []
        for _ in range(64):  # branch-relaxation fixpoint: sizes depend on addresses and vice versa
            plan, new_labels = self._layout(program, labels)
            if new_labels == labels:
                labels = new_labels
                break
            labels = new_labels
        else:
            raise OutOfSpace("layout did not converge")

        edits: dict[int, int] = {}
        for addr, kind, data in plan:
            words = _encode_branch(addr, data, 0xE, 0) if kind == "cj" else _encode(addr, data, labels)
            for k, w in enumerate(words):
                edits[addr + k] = w
        if covered is not None:
            bad = [a for a in edits if covered(a)]
            if bad:
                raise ValueError(f"injection would touch {len(bad)} signature-covered words, e.g. {bad[0]:#x}")
        return edits, labels, self.slots[0].addr

    def _layout(self, program, labels):
        plan, new_labels, pending = [], {}, []
        slot, cursor = 0, self.slots[0].addr

        def slot_tail():
            return self.slots[slot].addr + self.slots[slot].length

        for item in program:
            if item[0] == "label":
                pending.append(item[1])
                continue
            size = len(_encode(cursor, item, labels))
            if cursor + size > slot_tail() - CJ_RESERVE:
                if slot + 1 >= len(self.slots):
                    raise OutOfSpace("not enough slots for this program")
                target = self.slots[slot + 1].addr
                plan.append((cursor, "cj", target))
                slot, cursor = slot + 1, self.slots[slot + 1].addr
            for name in pending:
                new_labels[name] = cursor
            pending = []
            plan.append((cursor, "instr", item))
            cursor += size
        for name in pending:
            new_labels[name] = cursor
        return plan, new_labels


def trampoline(user: list, original_target: int, scratch: int = 0xC700) -> list:
    """Wrap `user` so it preserves AL/AH/X, runs, then tail-calls the replaced function."""
    return [
        ("st", "AL", "abs", scratch), ("st", "AH", "abs", scratch + 1), ("st", "X", "abs", scratch + 2),
        *user,
        ("ld", "AL", "abs", scratch), ("ld", "AH", "abs", scratch + 1), ("ld", "X", "abs", scratch + 2),
        ("bra_abs", original_target),  # tail-call: replaced function returns to the original caller
    ]


def repoint_call(words, call_addr: int, new_target: int) -> dict[int, int]:
    """Redirect an existing bsr at call_addr to new_target, in place and same length."""
    original = _encode(call_addr, ("call_abs", _bsr_target(words, call_addr)), {})
    redirect = _encode_branch(call_addr, new_target, 9, 3)
    if len(redirect) != len(original):
        raise ValueError(f"repoint needs {len(redirect)} words but the call is {len(original)}; "
                         "place the entry slot at a matching distance")
    return {call_addr + k: w for k, w in enumerate(redirect)}


def _bsr_target(words, addr: int) -> int:
    """Decode the target of the bsr instruction at addr (for same-length checks)."""
    pre = npre = 0
    i = addr
    while (words[i] & 0xFF) == 0 and words[i] != 0:
        opd = words[i] >> 8
        pre = (pre << 8 | opd) if npre else opd
        npre += 1
        i += 1
    opd = words[i] >> 8
    val = (opd - 0x100 if opd & 0x80 else opd)
    if npre:
        p = pre & 0xFF
        val += (p - 0x100 if p & 0x80 else p) << 8
    if npre == 2:
        p = (pre >> 8) & 0xFF
        val += (p - 0x100 if p & 0x80 else p) << 16
    return i + val
