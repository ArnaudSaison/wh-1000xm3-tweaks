#!/usr/bin/env python3
"""A XAP interpreter with a real stack and message queue, for running vm.app's event flows.

tools/xap_emulator.py runs one function with stubbed frames. This one runs whole flows:
a button press enters the real engine handler, which posts real messages, which reach
the real application handler, and so on. Time only moves when the queue is advanced.

What is real and what is a model:

* Every instruction executed comes from the firmware image under test.
* Firmware calls are models: alloc (9), send message (16), cancel message (17), and
  zero for everything else, each recorded in `trap_log`.
* Functions named in `stubs` are replaced by Python, for things with effects that can't
  be modelled here, such as playing a sound.
* X is the link register, as in Sony's code: leaf functions end in rts (bra @(0,X)) and
  never touch X, and every function that uses X opens a frame first. A call leaves the
  return address in X, and rts refuses to run if X was changed.
* RAM starts as zero except for what a scenario sets up. A real headset has run its whole
  boot first, so a flow that works here can still behave differently on hardware.

An instruction this file can't execute raises Unmodelled rather than guessing.
"""
from __future__ import annotations

import heapq
import struct
from pathlib import Path
from typing import Callable

HEADER_WORDS = 0x18
CODE_WORDS = 0x35D2B
CODE_LIMIT = HEADER_WORDS + CODE_WORDS
DATA_DELTA = 0x2E943      # data address D is file word D + DATA_DELTA, for constants
TRAP_ENTRY = 0x286
STUB_LOW, STUB_HIGH = 0x40, 0x286
REG_NAMES = ["AH", "AL", "X", "Y"]
REGISTER_MIRRORS = {0xFFFFE0: "AH", 0xFFFFE1: "AL", 0xFFFFE3: "X", 0xFFFFE4: "Y"}
STACK_TOP = 0x3000
HEAP_BASE = 0x4000
IMMEDIATE = 0xFFFFFFFF    # a delay of all ones means "deliver now"
RETURN_TO_HOST = -1
TRAP_ALLOC = 9
TRAP_SEND = 16
TRAP_CANCEL = 17


class Unmodelled(Exception):
    pass


def sx8(value: int) -> int:
    return value - 0x100 if value & 0x80 else value


def s16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def load_words(path: Path) -> list[int]:
    data = Path(path).read_bytes()
    return list(struct.unpack(f">{len(data) // 2}H", data[:len(data) // 2 * 2]))


class Machine:
    def __init__(self, words: list[int], handlers: dict[int, int] | None = None):
        self.words = words
        self.ram: dict[int, int] = {}
        self.AH = self.AL = self.X = 0
        self.Y = STACK_TOP
        self.Z = self.N = self.C = False
        self.unsigned_next = False
        self.heap = HEAP_BASE
        self.now = 0
        self.sequence = 0
        self.queue: list[tuple[int, int, int, int, int]] = []  # (time, order, task, id, payload)
        self.delivered: list[tuple[int, int, int]] = []        # (time, task, id)
        self.trap_log: list[int] = []
        self.calls: list[tuple[int, int, int]] = []            # (time, function, AL) for stubs
        self.stubs: dict[int, Callable[["Machine"], None]] = {}
        self.handlers = handlers or {}
        self.max_steps = 2_000_000
        self.steps = 0

    # --- memory ---------------------------------------------------------------
    def read(self, address: int) -> int:
        address &= 0xFFFFFF
        name = REGISTER_MIRRORS.get(address)
        if name:
            return getattr(self, name)
        if address < 0x10000:
            file_word = address + DATA_DELTA
            if CODE_LIMIT <= file_word < len(self.words):
                return self.words[file_word]
        return self.ram.get(address, 0)

    def write(self, address: int, value: int) -> None:
        address &= 0xFFFFFF
        value &= 0xFFFF
        name = REGISTER_MIRRORS.get(address)
        if name:
            setattr(self, name, value)
            return
        if address < 0x10000 and CODE_LIMIT <= address + DATA_DELTA < len(self.words):
            raise Unmodelled(f"write to constant data at {address:#x}")
        self.ram[address] = value

    def alloc(self, size: int) -> int:
        pointer = self.heap
        self.heap += max(size, 1)
        return pointer

    # --- firmware calls -------------------------------------------------------
    def _trap_number(self, stub: int) -> int:
        for address in range(stub, stub + 8):
            word = self.words[address]
            prefix = self.words[address - 1]
            if ((word >> 4) & 15) == 1 and ((word >> 2) & 3) == 2 and word & 3 == 0:
                value = sx8(word >> 8)
                if prefix & 0xFF == 0 and prefix:
                    value += sx8(prefix >> 8) << 8
                return value & 0xFFFF
        raise Unmodelled(f"no firmware call number in stub {stub:#x}")

    def _trap(self, number: int) -> None:
        self.trap_log.append(number)
        if number == TRAP_ALLOC:
            self.AL = self.alloc(self.AL)
        elif number == TRAP_SEND:
            payload = self.read(self.Y)
            delay = (self.read(self.Y + 1) << 16) | self.read(self.Y + 2)
            self.send(self.AL, self.AH, payload, 0 if delay == IMMEDIATE else delay)
        elif number == TRAP_CANCEL:
            self.AL = 1 if self.cancel(self.AL, self.AH) else 0
        else:
            self.AL = 0

    def send(self, task: int, ident: int, payload: int = 0, delay: int = 0) -> None:
        self.sequence += 1
        heapq.heappush(self.queue, (self.now + delay, self.sequence, task & 0xFFFF, ident & 0xFFFF, payload))

    def cancel(self, task: int, ident: int) -> bool:
        for item in sorted(self.queue):
            if item[2] == (task & 0xFFFF) and item[3] == (ident & 0xFFFF):
                self.queue.remove(item)
                heapq.heapify(self.queue)
                return True
        return False

    def pending(self, task: int, ident: int) -> bool:
        return any(q[2] == (task & 0xFFFF) and q[3] == (ident & 0xFFFF) for q in self.queue)

    def run_until(self, time_ms: int) -> None:
        """Deliver every message due by this time, in order."""
        while self.queue and self.queue[0][0] <= time_ms:
            when, _, task, ident, payload = heapq.heappop(self.queue)
            self.now = when
            self.delivered.append((when, task, ident))
            handler = self.handlers.get(task)
            if handler is None:
                continue  # a task this scenario doesn't model
            self.write(self.Y, payload)
            self.AL, self.AH = task, ident
            self.call(handler)
        self.now = max(self.now, time_ms)

    # --- execution ------------------------------------------------------------
    def call(self, address: int) -> None:
        """Run from address until it returns to the host. Y is left as it was."""
        saved_y = self.Y
        self.X = RETURN_TO_HOST & 0xFFFF
        stack: list[int] = [RETURN_TO_HOST]
        pc = address
        while pc != RETURN_TO_HOST:
            self.steps += 1
            if self.steps > self.max_steps:
                raise Unmodelled(f"step limit at {pc:#x}")
            pc = self._step(pc, stack)
        if self.Y != saved_y:
            raise Unmodelled(f"stack imbalance after {address:#x}: {self.Y:#x} != {saved_y:#x}")

    def _operand_address(self, mode: int, value: int) -> int:
        if mode == 1:
            return value & 0xFFFFFF
        return (value + (self.X if mode == 2 else self.Y)) & 0xFFFF

    def _flags(self, value: int) -> None:
        self.Z = (value & 0xFFFF) == 0
        self.N = bool(value & 0x8000)

    def _step(self, pc: int, stack: list[int]) -> int:
        if not 0 <= pc < CODE_LIMIT:
            raise Unmodelled(f"execution left the code segment: {pc:#x}")
        prefix = count = 0
        while True:
            word = self.words[pc]
            if word & 0xFF == 0 and (word != 0 or count):
                prefix = (prefix << 8 | word >> 8) if count else word >> 8
                count += 1
                pc += 1
                continue
            break
        word = self.words[pc]
        following = pc + 1
        mode, reg, opcode, operand = word & 3, (word >> 2) & 3, (word >> 4) & 15, word >> 8
        value = sx8(operand)
        if count:
            value += sx8(prefix & 0xFF) << 8
        if count == 2:
            value += sx8((prefix >> 8) & 0xFF) << 16
        unsigned = self.unsigned_next
        self.unsigned_next = False

        if word == 0x0000:
            return following
        if word == 0x00E2:  # rts is "bra @(0,X)": it only works if X still holds the return address
            target = stack.pop()
            if self.X != target & 0xFFFF:
                raise Unmodelled(f"rts at {pc:#x} after X was changed")
            return target
        if word == 0x0009:  # u: the next shift or multiply treats its operands as unsigned
            self.unsigned_next = True
            return following
        if word == 0xFE09:  # brxl: X is relative to the following word
            return following + s16(self.X)
        if opcode == 0:
            if word & 0xFF == 0x0B:  # enter
                self.Y = (self.Y + sx8(operand)) & 0xFFFF
                return following
            if word & 0xFF == 0x0F:  # leave: restore the return address into X, and go there
                self.Y = (self.Y - sx8(operand)) & 0xFFFF
                target = stack.pop()
                self.X = target & 0xFFFF
                return target
            raise Unmodelled(f"system instruction {word:#06x} at {pc:#x}")
        if opcode == 9:
            if reg == 3:
                if mode != 0:
                    raise Unmodelled(f"indirect call at {pc:#x}")
                return self._call(pc + value, following, stack)
            if reg == 2:
                source = value if mode == 0 else self.read(self._operand_address(mode, value))
                self._flags(source)
                return following
            if reg == 0 and mode == 0:  # smult: 32-bit product, low half in AL, high in AH
                left = self.AL if unsigned else s16(self.AL)
                right = (value & 0xFFFF) if unsigned else s16(value)
                product = left * right
                self.AL, self.AH = product & 0xFFFF, (product >> 16) & 0xFFFF
                self._flags(self.AL)
                return following
            raise Unmodelled(f"opcode 9 form at {pc:#x}")
        if opcode == 0xA:  # shifts act on AH:AL together
            combined = (self.AH << 16) | self.AL
            count_bits = value & 0x1F
            if reg == 0:
                combined = (combined << count_bits) & 0xFFFFFFFF
            elif reg == 1:
                if unsigned:
                    combined >>= count_bits
                else:
                    signed = combined - (1 << 32) if combined & 0x80000000 else combined
                    combined = (signed >> count_bits) & 0xFFFFFFFF
            else:
                raise Unmodelled(f"rotate at {pc:#x}")
            self.AH, self.AL = combined >> 16, combined & 0xFFFF
            self._flags(self.AL)
            return following
        if opcode in (0xE, 0xF):
            if mode != 0:
                raise Unmodelled(f"indirect branch at {pc:#x}")
            take = {(0xE, 0): True, (0xE, 1): self.N, (0xE, 2): not self.N, (0xE, 3): self.N,
                    (0xF, 0): not self.Z, (0xF, 1): self.Z, (0xF, 2): not self.C, (0xF, 3): self.C}[(opcode, reg)]
            return pc + value if take else following
        if opcode == 2:
            if mode == 0:  # conditional branches after cmp; the meaning is inferred from use
                return pc + value if self._compare_branch(reg, pc) else following
            self.write(self._operand_address(mode, value), getattr(self, REG_NAMES[reg]))
            return following
        if opcode in (1, 3, 4, 5, 6, 7, 8, 0xB, 0xC, 0xD):
            source = value & 0xFFFF if mode == 0 else self.read(self._operand_address(mode, value))
            name = REG_NAMES[reg]
            current = getattr(self, name)
            if opcode == 1:
                result = source
            elif opcode == 3:
                total = current + source
                self.C = total > 0xFFFF
                result = total
            elif opcode == 4:
                total = current + source + int(self.C)
                self.C = total > 0xFFFF
                result = total
            elif opcode == 5:
                self.C = current < source
                result = current - source
            elif opcode == 6:
                total = current - source - int(self.C)
                self.C = total < 0
                result = total
            elif opcode == 7:  # nadd: source minus register
                self.C = source < current
                result = source - current
            elif opcode == 8:
                self.C = current < source
                self.compared = (current, source)
                self._flags(current - source)
                return following
            elif opcode == 0xB:
                result = current | source
            elif opcode == 0xC:
                result = current & source
            else:
                result = current ^ source
            setattr(self, name, result & 0xFFFF)
            self._flags(result)
            return following
        raise Unmodelled(f"opcode {opcode:#x} (word {word:#06x}) at {pc:#x}")

    compared = (0, 0)

    def _compare_branch(self, reg: int, pc: int) -> bool:
        """Signed, unlike bcc/bcs: at 0x11890, -1 means "no input" and must not count as above 14."""
        a, b = s16(self.compared[0]), s16(self.compared[1])
        return {0: a > b, 1: a >= b, 2: a < b, 3: a <= b}[reg]

    def _call(self, target: int, following: int, stack: list[int]) -> int:
        """X is the link register: bsr puts the return address in it, so no value in X survives a call."""
        stub = self.stubs.get(target)
        if stub is not None:
            self.calls.append((self.now, target, self.AL))
            stub(self)
            self.X = following & 0xFFFF
            return following
        if STUB_LOW <= target < STUB_HIGH:
            self._trap(self._trap_number(target))
            self.X = following & 0xFFFF
            return following
        stack.append(following)
        self.X = following & 0xFFFF
        return target
