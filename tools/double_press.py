#!/usr/bin/env python3
"""Enter pairing with a quick double press of the power button while the headset is on.

    uv run tools/double_press.py plan  firmware.bin
    uv run tools/double_press.py build firmware.bin out.bin --hold-ms 4000

How it works (see docs/double-press.md):

* The engine already sends event 0x12 when the power button is released before any hold
  timer fires. In the normal on state, the app answers with a battery announcement, by
  calling 0x89AB at 0x92D1. Those call words aren't covered by the signature.
* That call is repointed at new code. The code cancels a private timer message:
  - nothing was pending: first click. Start the timer and announce the battery as usual.
  - a timer was pending: second click inside the window. Stop reconnecting and leave the
    connections, in the order the power-off path uses. Then post the click event to the
    application again, three seconds later, with a second private timer as a marker.
  - the marker was pending: this is that posted click. Play the pairing sound and call
    0x11D84(1), the one function behind every pairing start.
* The wait is there for the sounds. When the phone's link drops, the firmware says
  "Bluetooth disconnected" (0x12FC0), and a new sound cuts off the one playing. On a real
  headset the first version, which did everything at once, had its pairing sound cut off
  that way. Sony's own on-to-pairing handler also waits three seconds.
* The timer holds the state, so no RAM is needed. The message goes to the button engine's
  task with an id its handler ignores.
* This only runs after a short click in the on state, not at boot. A bug in it should
  show up only on a click, not stop the headset starting.

The new code replaces unsigned words of functions nothing refers to: no call, no branch,
no fall-through, and no pointer to them in any form in the code or constants. That is
strong evidence they are unused, not a proof. See `unreferenced_functions`.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dfu_sign  # noqa: E402
from xap_inject import Linker, Slot, _encode_branch  # noqa: E402
from xap_machine import Machine  # noqa: E402
from xapanalyze import VmApp  # noqa: E402

HOOK_CALL = 0x92D1               # bsr 0x89AB, event 0x12 in the normal on state
HOOK_WORDS = (0x92D0, 0x92D1)    # the call's prefix and bsr. Nothing else at the site changes
BATTERY = 0x89AB
CODE_LIMIT = 0x18 + 0x35D2B      # from the vm.app header. Execution must stay below this
POINTER_OFFSET = 0x18            # task structures store code addresses minus the header
# All within 32K of each other, so every jump between fragments uses a form Sony's code uses:
# far calls are common in vm.app, but it has no far bra.
SPACE_FUNCTIONS = (0x034EE, 0x03666, 0x03968, 0x05ACC, 0x05B20, 0x06109, 0x07963)
MIN_SLOT = 6
SEND_LATER = 0x10A81             # args: AL task, AH id, (0,Y) payload, (1,Y):(2,Y) delay
CANCEL_ALL = 0x35756             # args: AL task, AH id. Returns the number cancelled
PLAY_SOUND = 0x14EA1
STOP_RECONNECT = 0x12F4E
LEAVE_CONNECTIONS = 0x12040
START_CONNECTION_MODE = 0x11D84  # 1: pairing, 0: normal
CONNECTION_OBJECT = 0x588
ENGINE_TASK = 0x8E47
ENGINE_TASK_OPERAND = ENGINE_TASK - 0x10000  # Sony loads it as a negative 16-bit value, with one prefix
PRIVATE_ID = 0x7F                # the engine's handler ignores ids above 5
MARKER_ID = 0x7E                 # pending while the connections drop and the pairing start is on its way
APP_TASK = 0x7DCA
CLICK_EVENT = 0x12
PAIRING_DELAY_MS = 3000          # Sony's 0x10BDD waits this long between its sound and 0x11D84(1)
MARKER_MS = 10000                # long enough to outlast the delay; it only expires if the click is dropped
STATE_NORMAL = 1
SOUND_PAIRING = 8
DEFAULT_WINDOW_MS = 500
FRAME_ENTER, FRAME_LEAVE = 0xFB0B, 0xFB0F  # 3 locals, used as a call's stack arguments


def program(window_ms: int = DEFAULT_WINDOW_MS) -> list:
    return [
        ("raw", FRAME_ENTER),
        ("ld", "X", "abs", CONNECTION_OBJECT),
        ("ld", "AL", "X", 1),
        ("cmp", "AL", "#", STATE_NORMAL),
        ("bne", "battery"),
        ("ld", "AL", "#", ENGINE_TASK_OPERAND),
        ("ld", "AH", "#", MARKER_ID),
        ("call_abs", CANCEL_ALL),
        ("cmp", "AL", "#", 0),
        ("bne", "pair"),
        ("ld", "AL", "#", ENGINE_TASK_OPERAND),
        ("ld", "AH", "#", PRIVATE_ID),
        ("call_abs", CANCEL_ALL),
        ("cmp", "AL", "#", 0),
        ("bne", "drop"),
        ("st", "AL", "Y", 0),
        ("st", "AL", "Y", 1),
        ("ld", "AL", "#", window_ms),
        ("st", "AL", "Y", 2),
        ("ld", "AL", "#", ENGINE_TASK_OPERAND),
        ("ld", "AH", "#", PRIVATE_ID),
        ("call_abs", SEND_LATER),
        ("label", "battery"),
        ("ld", "AL", "#", 0),
        ("ld", "AH", "#", 0),
        ("call_abs", BATTERY),
        ("raw", FRAME_LEAVE),
        ("label", "drop"),
        ("call_abs", STOP_RECONNECT),
        ("call_abs", LEAVE_CONNECTIONS),
        ("ld", "AL", "#", 0),
        ("st", "AL", "Y", 0),
        ("st", "AL", "Y", 1),
        ("ld", "AL", "#", MARKER_MS),
        ("st", "AL", "Y", 2),
        ("ld", "AL", "#", ENGINE_TASK_OPERAND),
        ("ld", "AH", "#", MARKER_ID),
        ("call_abs", SEND_LATER),            # it reads its three stack arguments and leaves them as they are
        ("ld", "AL", "#", PAIRING_DELAY_MS),
        ("st", "AL", "Y", 2),
        ("ld", "AL", "#", APP_TASK),
        ("ld", "AH", "#", CLICK_EVENT),
        ("call_abs", SEND_LATER),
        ("raw", FRAME_LEAVE),
        ("label", "pair"),
        ("ld", "AL", "#", SOUND_PAIRING),
        ("call_abs", PLAY_SOUND),
        ("ld", "AL", "#", 1),
        ("call_abs", START_CONNECTION_MODE),
        ("raw", FRAME_LEAVE),
    ]


class PatchError(ValueError):
    pass


# --- running clicks through the real engine and application handler, in the emulator ---

POWER_TASK, CONNECTION_TASK = 0x8D16, 0x8E54
FLOW_HANDLERS = {ENGINE_TASK: 0x1153C, APP_TASK: 0x90EE}
POWER_HANDLER = 0x10831
LED_PATTERN, DISCOVERABLE = 0xF619, 0x8AB1
BUTTON_POWER = 2  # the mask for input 1 in the modelled input map; only the engine's own mapping reads it


def _nothing(machine) -> None:
    pass


def _zero(machine) -> None:
    machine.AL = 0


def flow_machine(words) -> Machine:
    """A machine in the normal on state, with the functions that reach hardware replaced by stubs."""
    m = Machine(words, dict(FLOW_HANDLERS))
    objects = {0x54B: 0x20, 0x54E: 0x20, 0x54F: 0x20, 0x552: 0x60, 0x567: 0x10, 0x56F: 0x80, 0x57F: 0x20,
               0x581: 0x10, 0x586: 0x20, 0x588: 0x20, 0x58C: 0x10, 0x58D: 0x10, 0x58E: 0x10, 0x591: 0x10}
    for pointer, size in objects.items():
        m.write(pointer, m.alloc(size))
    m.write(m.read(0x586) + 0, APP_TASK)  # where the engine sends events: 0x95CA passes this to 0x115CD
    m.write(m.read(0x586) + 3, 0xFFFF)    # no input down
    m.write(m.read(0x586) + 4, 0xFFFF)    # no pending click
    m.write(m.read(0x588) + 1, 1)         # normal on state
    m.write(m.read(0x581) + 0, 1)         # normal power mode
    m.write(m.read(0x54E) + 0, 0)
    config = m.read(0x552) + 7            # the input map the engine reads through 0xAAD9, 4 words per entry
    for i in range(0x40):                 # the firmware's default map: bit n is input n
        m.write(config + i, m.read(0x801A + i))
    m.write(config + 0x34, 0)             # no combination-button remaps
    m.write(config + 0x38, 0)
    m.stubs.update({
        BATTERY: _nothing,
        PLAY_SOUND: _nothing,
        LED_PATTERN: _nothing,
        LEAVE_CONNECTIONS: _nothing,
        0x11175: _nothing,       # audio mode switching
        0xF667: _nothing,        # LED refresh
        0xF67C: _nothing,
        0x753F: _nothing,        # notifies other modules of a state change
        0x8983: _nothing,
        0x11705: _nothing,
        0xA330: _nothing,
        0x10CB5: _nothing,
        DISCOVERABLE: _nothing,  # discoverability, firmware side
        0x120C6: _nothing,
        0xABEC: _zero,           # pairing timeout: 0 uses the 5 minute timer
    })
    return m


def flow_press(m: Machine, at_ms: int, held_ms: int) -> None:
    """Drive the engine's own debounced input message: mask down, then mask up."""
    m.run_until(at_ms)
    down = m.alloc(1)
    m.write(down, BUTTON_POWER)
    m.send(ENGINE_TASK, 0, down, 0)
    m.run_until(at_ms + held_ms)
    up = m.alloc(1)
    m.write(up, 0)
    m.send(ENGINE_TASK, 0, up, 0)
    m.run_until(at_ms + held_ms)


def flow_count(m: Machine, function: int, argument: int | None = None) -> int:
    return sum(1 for _, f, al in m.calls if f == function and (argument is None or al == argument))


def flow_state(m: Machine) -> int:
    return m.read(m.read(CONNECTION_OBJECT) + 1)


def rehearse(words, window_ms: int = DEFAULT_WINDOW_MS, short_ms: int = 1500) -> list[dict]:
    """Click the power button on a built file, through the real engine and application handler.

    Each row says what was expected and whether it happened. This shows the logic, not the radio:
    sound, discoverability and dropping the connections are stubs.
    """
    click = min(100, short_ms // 4)
    late = window_ms + 300
    cases = [
        (f"Double click: battery, connections dropped, pairing {PAIRING_DELAY_MS / 1000:g} s later",
         [(0, click), (window_ms - 50, click)], (1, 1, 2)),
        ("One click: battery only", [(0, click)], (1, 0, 1)),
        (f"Second click {late} ms after the first: battery twice, no pairing",
         [(0, click), (click + late, click)], (2, 0, 1)),
        ("Click, then a hold: no pairing", [(0, click), (click + 200, short_ms + 300)], (1, 0, 1)),
    ]
    rows = []
    for label, presses, expected in cases:
        m = flow_machine(words)
        try:
            for at, held in presses:
                flow_press(m, at, held)
            released = presses[-1][0] + presses[-1][1]
            m.run_until(released + PAIRING_DELAY_MS - 1)
            early = flow_state(m)
            m.run_until(released + PAIRING_DELAY_MS + window_ms + 500)
            got = (flow_count(m, BATTERY), flow_count(m, PLAY_SOUND, SOUND_PAIRING), flow_state(m))
            order = [(when, f) for when, f, al in m.calls
                     if (f, al) in ((PLAY_SOUND, SOUND_PAIRING), (DISCOVERABLE, 1)) or f == LEAVE_CONNECTIONS]
            ok = got == expected and early == 1
            ok = ok and not m.pending(ENGINE_TASK, PRIVATE_ID) and not m.pending(ENGINE_TASK, MARKER_ID)
            if expected[2] == 2:
                ok = ok and order == [(released, LEAVE_CONNECTIONS), (released + PAIRING_DELAY_MS, PLAY_SOUND),
                                      (released + PAIRING_DELAY_MS, DISCOVERABLE)]
            detail = f"battery x{got[0]}, pairing sound x{got[1]}, state {got[2]}"
        except Exception as err:  # the emulator refusing to run something is a failed rehearsal, not a crash
            ok, detail = False, f"emulator stopped: {err}"
        rows.append({"label": f"Double press: {label}", "ok": ok, "detail": detail})
    return rows


def _layout(image: bytes):
    s = dfu_sign.parse_sections(image)
    return dfu_sign.VmLayout(dfu_sign.words_le(image[s.fs[0]:s.fs[0] + s.fs[1]]))


def _branch_targets(app: VmApp) -> set[int]:
    targets = set()
    for x in app.ins:
        match = re.match(r"(bra|blt|bpl|bmi|bne|beq|bcc|bcs) (0x[0-9a-f]+)$", x[2])
        if match:
            targets.add(int(match[2], 16))
        match = re.match(r"print \w+, #(-?0x[0-9a-f]+)$", x[2])  # the conditional branches after cmp
        if match:
            targets.add(x[0] + int(match[1], 16))
        target = app.call_target(x)
        if target is not None:
            targets.add(target)
    return targets


def unreferenced_functions(app: VmApp) -> dict[int, int]:
    """Frame-entry functions nothing can reach, as {start: end}, by every test available here."""
    words = app.words
    constants = set(words[CODE_LIMIT:])
    immediates = {x[3] & 0xFFFF for x in app.ins if x[3] is not None}
    targets = _branch_targets(app)
    starts = [x[0] for x in app.ins if re.fullmatch(r"sys 0x[ef][0-9a-f]0b", x[2]) and x[0] < CODE_LIMIT]
    found = {}
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else CODE_LIMIT
        forms = {(start - POINTER_OFFSET) & 0xFFFF, start & 0xFFFF,
                 (start - POINTER_OFFSET - 1) & 0xFFFF, (start - 1) & 0xFFFF}
        if start in targets or forms & constants or forms & immediates:
            continue
        before = app.ins[app.index[start] - 1][2]
        if not (before == "rts" or re.fullmatch(r"sys 0x[ef][0-9a-f]0f", before) or before.startswith("bra ")):
            continue
        if any(start < t < end for t in _outside_targets(app, start, end)):
            continue
        found[start] = end
    return found


def _outside_targets(app: VmApp, start: int, end: int) -> list[int]:
    out = []
    for x in app.ins:
        if start <= x[0] < end:
            continue
        match = re.match(r"(bra|blt|bpl|bmi|bne|beq|bcc|bcs) (0x[0-9a-f]+)$", x[2])
        if match:
            out.append(int(match[2], 16))
        match = re.match(r"print \w+, #(-?0x[0-9a-f]+)$", x[2])
        if match:
            out.append(x[0] + int(match[1], 16))
        target = app.call_target(x)
        if target is not None:
            out.append(target)
    return out


def code_slots(app: VmApp, covered) -> list[Slot]:
    found = unreferenced_functions(app)
    slots = []
    for function in SPACE_FUNCTIONS:
        if function not in found:
            raise PatchError(f"{function:#x} isn't unreferenced in this firmware")
        address, end = function, found[function]
        while address < end:
            if covered(address):
                address += 1
                continue
            run = address
            while address < end and not covered(address):
                address += 1
            if address - run >= MIN_SLOT:
                slots.append(Slot(run, address - run))
    return slots


def build_edits(app: VmApp, covered, window_ms: int = DEFAULT_WINDOW_MS) -> tuple[dict[int, int], dict]:
    if any(covered(w) for w in HOOK_WORDS):
        raise PatchError("the hook words are signed in this firmware")
    if app.call_target(app.ins[app.index[HOOK_CALL]]) != BATTERY:
        raise PatchError("the hook isn't where it was expected: this firmware is different")
    if app.words[0x92CF] != 0x0010 or app.words[0x92CE] != 0x0014:
        raise PatchError("the words before the hook aren't the expected ld AL,#0 and ld AH,#0")
    slots = code_slots(app, covered)
    code, labels, entry = Linker(slots).link(program(window_ms), covered=covered)
    if any(address >= CODE_LIMIT for address in code):
        raise PatchError("the new code would leave the code segment")
    if max(code) - min(code) >= 0x8000:
        raise PatchError("the fragments are too far apart for near jumps")
    edits = dict(code)
    call = _encode_branch(HOOK_WORDS[0], entry, 9, 3)
    if len(call) != len(HOOK_WORDS):
        raise PatchError("the call to the new code doesn't fit the two words of the original call")
    for address, word in zip(HOOK_WORDS, call):
        edits[address] = word
    strange = foreign_shapes(app, edits)
    if strange:
        address, shape = strange[0]
        raise PatchError(f"the instruction at {address:#x} has a form Sony's code never uses: {shape}")
    used = sum(any(s.addr <= a < s.addr + s.length for a in code) for s in slots)
    return edits, {"entry": entry, "fragments": used, "code_words": len(code), "labels": labels}


def instruction_shapes(words, start: int, end: int) -> list[tuple[int, tuple]]:
    """(address, shape) for each instruction in [start, end). A shape is the number of prefix
    words and the opcode byte, plus the whole word for frame instructions (opcode 0)."""
    shapes, prefixes, first = [], 0, start
    for address in range(start, end):
        word = words[address]
        if word & 0xFF == 0 and (word != 0 or prefixes):
            prefixes += 1
            continue
        low = word & 0xFF
        shapes.append((first, (prefixes, low, word) if low >> 4 == 0 else (prefixes, low)))
        prefixes, first = 0, address + 1
    return shapes


def foreign_shapes(app: VmApp, edits: dict[int, int]) -> list[tuple[int, tuple]]:
    """Instructions in the edits whose encoding form doesn't occur anywhere in the firmware's own code.

    The emulator and the real chip could disagree on a form nobody has seen run. Sticking to
    forms Sony's compiler emitted keeps the patch on ground the headset already executes.
    """
    known = {shape for _, shape in instruction_shapes(app.words, 0, CODE_LIMIT)}
    patched = apply_to_words(list(app.words), edits)
    found, addresses = [], sorted(edits)
    run_start = addresses[0]
    for previous, address in zip(addresses, addresses[1:] + [None]):
        if address is not None and address == previous + 1:
            continue
        found += [(a, shape) for a, shape in instruction_shapes(patched, run_start, previous + 1)
                  if shape not in known]
        run_start = address
    return found


def apply_to_words(words: list[int], edits: dict[int, int]) -> list[int]:
    out = list(words)
    for address, value in edits.items():
        out[address] = value
    return out


def _vm_words(image: bytes) -> list[int]:
    s = dfu_sign.parse_sections(image)
    words = dfu_sign.words_le(image[s.fs[0]:s.fs[0] + s.fs[1]])
    layout = dfu_sign.VmLayout(words)
    return words[layout.start:layout.end]


def app_from_image(image: bytes, scratch: Path) -> VmApp:
    words = _vm_words(image)
    scratch.write_bytes(struct.pack(f">{len(words)}H", *words))
    return VmApp(scratch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "build"))
    parser.add_argument("firmware", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--window-ms", type=int, default=DEFAULT_WINDOW_MS)
    parser.add_argument("--hold-ms", type=int, default=4000, help="keep the shorter pairing hold; 0 leaves Sony's")
    args = parser.parse_args()
    if not 150 <= args.window_ms <= 1500:
        parser.error("--window-ms must be between 150 and 1500")
    image = args.firmware.read_bytes()
    layout = _layout(image)
    scratch = Path(sys.argv[0]).with_suffix(".vmtmp")
    try:
        app = app_from_image(image, scratch)
        edits, info = build_edits(app, layout.covered, args.window_ms)
    except PatchError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    finally:
        scratch.unlink(missing_ok=True)
    print(f"entry {info['entry']:#x}, {info['fragments']} fragments, {info['code_words']} code words, "
          f"{len(edits)} words edited")
    if args.command == "plan":
        for address in sorted(edits):
            print(f"  {address:#07x} = {edits[address]:#06x}")
        return 0
    if args.output is None:
        parser.error("build needs an output file")
    all_edits = dict(edits)
    if args.hold_ms:
        all_edits.update(dfu_sign.pairing_edits(image, args.hold_ms))
    try:
        output = dfu_sign.patch(image, all_edits)
    except dfu_sign.SignError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    args.output.write_bytes(output)
    print(f"wrote {args.output}: signatures and checksums pass offline")
    print(f"sha1 {hashlib.sha1(output).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
