#!/usr/bin/env python3
"""Build a web page that shows vm.app word by word, with emulator runs lighting up.

    uv run tools/firmware_viz.py --open

The page holds what each word is (code, constants, signed or not), addresses, and which
words each emulator run executed. It holds no firmware bytes and no disassembly.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tests")]

import dfu_sign  # noqa: E402
import double_press as dp  # noqa: E402
import test_double_press as flows  # noqa: E402
from xap_machine import CODE_LIMIT, Machine, Unmodelled  # noqa: E402

TEMPLATE = Path(__file__).with_name("firmware_viz_template.html")
HOLD_MS = 4000
KINDS = ["header", "code", "unused", "constants", "text", "zeros", "patch", "hook", "table"]
MIN_RUN = 4

FUNCTIONS = {
    0x1153C: "Button engine: message handler",
    0x1186D: "Button engine: input changed",
    0x11457: "Button engine: mask to input",
    0x1170C: "Button engine: hold timer fired",
    0x114FD: "Button engine: schedule one timer",
    0x11523: "Button engine: schedule an input's timers",
    0x114C9: "Button engine: press and click events",
    0x90EE: "App: event handler",
    0x89AB: "Battery announcement",
    0x10A81: "Send message later",
    0x35756: "Cancel messages",
    0x3574C: "Send message now",
    0x14EA1: "Play sound",
    0x11D84: "Start connection mode",
    0x1259E: "Set connection state",
    0x12913: "Leave pairing",
    0x12F4E: "Stop reconnecting",
    0x10831: "Power: message handler",
    0x10B0C: "Power: start selected mode",
    0x107F6: "Power: message helper",
    0x9063: "App: event preprocessing",
    0x9A73: "App: long press flag",
    0x9DC9: "App: event counters",
    0x97B5: "App: event gating",
    0x12B6F: "Reconnect timer",
    0x12B32: "Stop page scan",
    0xB02D: "Stored device check",
    0xC257: "Usage counter",
}
TASKS = {flows.ENGINE: "Engine", flows.APP: "App", flows.POWER: "Power"}
ENGINE_MESSAGES = {0: "Input changed", 1: "Hold timer fired", dp.PRIVATE_ID: "Double-press window closed"}
APP_EVENTS = {0x11: "Power button down", 0x12: "Short click", 0x13: "Held 1.5 s", 0x14: "Released after 1.5 s",
              0x15: "Long hold", 0x16: "Released after long hold"}
SOUNDS = {8: "Pairing announcement"}


# (area, name, kind, address, length, level, note). kind "code" is a file word and a function,
# with length None for "up to the next function"; "data" is a data address; "word" is a file word.
# Levels: "confirmed" was checked on the headset or against Sony's signatures, "read" was seen
# in the disassembly, "inferred" is a reasoned interpretation.
FINDINGS = [
    ("Buttons", "Hold timing table", "data", 0x8DD8, 81, "confirmed", "Duration, kind and event for each input. Changing input 1's long hold worked on the headset."),
    ("Buttons", "Timing table pointers", "data", 0x8E29, 18, "confirmed", "A pointer and count per input. Moving input 1's pointer worked on the headset."),
    ("Buttons", "Press and click events", "data", 0x8DBA, 30, "read", "Each input's event on press, and on release if no hold timer fired."),
    ("Buttons", "Input map", "data", 0x801A, 52, "read", "Maps bit n of the button mask to input n, 4 words per entry."),
    ("Buttons", "Engine message handler", "code", 0x1153C, None, "read", "Receives input changes and timer messages."),
    ("Buttons", "Input changed", "code", 0x1186D, None, "read", "Cancels old timers, sends click events and starts hold timers."),
    ("Buttons", "Hold timer fired", "code", 0x1170C, None, "read", "Sends the event. Bit 15 entries reschedule themselves, which is auto-repeat."),
    ("Buttons", "App event switch", "data", 0x7E49, 0x41, "read", "Jump offsets for each button event."),
    ("Buttons", "App event handler", "code", 0x90EE, None, "read", "Where events reach Sony's application logic."),
    ("Buttons", "Battery on short click", "code", 0x89AB, None, "confirmed", "Called on event 0x12. The owner confirms a short click announces the battery."),
    ("Buttons", "Hidden sequence matcher", "code", 0x990F, None, "read", "Compares events with a stored list for 60 s. What the list means physically is unknown."),
    ("Buttons", "Hidden sequence list", "data", 0x7DDA, 14, "inferred", "Reaching the end enters a special state, possibly a service mode."),
    ("Buttons", "Touch gate", "code", 0xAC6B, None, "inferred", "Read by the analog path, which then ignores readings while touch is off."),
    ("Buttons", "Analog reading path", "code", 0x118F4, None, "read", "Matches analog readings against a ladder from settings. Probably touch."),
    ("LEDs", "Pattern list", "data", 0x8B43, 195, "confirmed", "39 descriptors. Pattern 23 matches the blink filmed during an update: 650 ms off, 650 ms on."),
    ("LEDs", "Pattern steps", "data", 0x8A33, 272, "read", "Pairs of LED mask and duration in ms."),
    ("LEDs", "Channel base pointers", "data", 0x8C06, 3, "read", "Where each of 3 channels' patterns begin."),
    ("LEDs", "Idle pattern picker", "code", 0xF75B, None, "read", "Chooses the pattern from the connection state."),
    ("LEDs", "Start a pattern", "code", 0xF619, None, "read", "Starts a numbered pattern directly."),
    ("LEDs", "Run a pattern step", "code", 0xF6A8, None, "read", "Applies a step, and schedules the next with a delayed message."),
    ("LEDs", "LED output", "code", 0xF685, None, "inferred", "Calls firmware call 171 for each channel in a mask."),
    ("Sound", "Sound table", "data", 0x998C, 243, "read", "File, alternative file and flag per sound. 8 is pairing, 11 connected."),
    ("Sound", "Sound filenames", "word", 0x37DA2, 1100, "read", "Names of the announcement files."),
    ("Sound", "Play a sound", "code", 0x14EA1, None, "read", "Called from about 40 places with a sound number."),
    ("Sound", "Sound table lookup", "code", 0x14F19, None, "read", "Reads the sound table directly."),
    ("Power and pairing", "Power message handler", "code", 0x10831, None, "read", "Switches on message id, through the table at data 0x8D1E."),
    ("Power and pairing", "Start selected mode", "code", 0x10B0C, None, "read", "Mode 2 calls the pairing start."),
    ("Power and pairing", "Mode switch table", "data", 0x8D3B, 9, "read", "Jump offsets for the power modes."),
    ("Power and pairing", "Start pairing or normal", "code", 0x11D84, None, "read", "Argument 1 sets state 2 and a 300 s timer. Reached from a long hold in the code."),
    ("Power and pairing", "Leave pairing", "code", 0x12913, None, "read", "Returns to state 1 when the timer fires."),
    ("Power and pairing", "Set connection state", "code", 0x1259E, None, "read", "Also refreshes the LED."),
    ("Power and pairing", "Pairing sound and state", "code", 0x10BDD, None, "read", "Plays sound 8, or the factory beep if a flag is set, and sets state 2."),
    ("Power and pairing", "Stored device check", "code", 0xB02D, None, "inferred", "Reads a setting that decides between normal start and pairing."),
    ("Updates", "Update file parser", "code", 0x33280, None, "confirmed", "Checks the file's structure. It accepted modified files on the headset."),
    ("Updates", "Update file tag names", "code", 0x32E7E, 12, "read", "Return APPUHDR2, PARTDATA and APPUPFTR."),
    ("Updates", "Validation hooks", "code", 0x32F3F, 4, "read", "Both simply return 1, so per-block validation is compiled out."),
    ("Updates", "Start the loader", "code", 0x32B60, None, "read", "Hands the code partition to the stack firmware, using firmware call 214."),
    ("Updates", "Loader result", "code", 0x32532, None, "read", "Records success or failure after the reboot."),
    ("Updates", "Report loader failure", "code", 0x32E11, None, "read", "Reports a failed loader run as error 0x1E. No file has been rejected on hardware yet."),
    ("Updates", "GAIA upgrade messages", "code", 0x3376B, 8, "inferred", "Refers to 0x6640 to 0x6642, which look like upgrade commands."),
    ("Signature", "vm.app checksum word", "word", 0x13, 1, "confirmed", "An XOR over the file, in an unsigned position. Updating it worked on the headset."),
    ("Signature", "File header", "word", 0, 0x18, "read", "Gives the code size and file length. Pointers to code are offset by its length."),
    ("Signature", "End of the code segment", "word", 0x35D39, 10, "read", "The last call target, just below the declared code size."),
    ("Protocol", "MDR frame unescaping", "code", 0x13800, 40, "read", "Handles the reserved bytes 3C, 3D and 3E."),
    ("Protocol", "MDR command dispatch", "word", 0x165FE, 8106, "read", "Compare constants for 41 of the app's commands appear here."),
    ("Protocol", "Touch panel setting", "code", 0x18055, None, "read", "Handles the MDR touch setting and sets a config bit."),
    ("Audio", "Volume caps", "code", 0x180AB, None, "read", "Picks hard-coded caps per mode: part of the regional limit."),
    ("Audio", "Firmware call stubs", "word", 0x40, 0x240, "read", "143 stubs that load a number and jump to 0x286."),
    ("Patch", "Moved hold table", "word", 0x394E9, 12, "confirmed", "Lives inside a debug string nothing reads. Running on the headset at 4 s."),
    ("Patch", "Double-press hook", "word", 0x92D0, 2, "confirmed", "Redirects the battery call. A double click enters pairing on the headset."),
]
LEVELS = ("confirmed", "read", "inferred")


def build_findings(app) -> list[dict]:
    """Resolve the list against the firmware, and refuse anything that doesn't line up."""
    frames = sorted(x[0] for x in app.ins if x[2].startswith("sys 0x") and x[2].endswith("0b"))
    out = []
    for area, name, kind, address, length, level, note in FINDINGS:
        if level not in LEVELS:
            raise SystemExit(f"bad level for {name}")
        start = address + app.delta if kind == "data" else address
        if kind == "code":
            if address not in app.index:
                raise SystemExit(f"{name}: {address:#x} isn't an instruction")
            if length is None:
                following = [f for f in frames if f > address]
                length = following[0] - address if following else 1
        if start < 0 or start + length > len(app.words):
            raise SystemExit(f"{name}: {address:#x} is outside vm.app")
        out.append({"area": area, "name": name, "start": start, "len": length, "level": level, "note": note})
    return out


class Tracer(Machine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace: list[int] = []
        self.marks: list[list] = []
        self.last_state = None

    def mark(self, kind: str, text: str) -> None:
        self.marks.append([len(self.trace), self.now, kind, text])

    def _step(self, pc, stack):
        self.trace.append(pc)
        return super()._step(pc, stack)

    def call(self, address):
        task, ident = self.AL, self.AH
        if task == flows.APP:
            self.mark("event", APP_EVENTS.get(ident, f"App event {ident:#04x}"))
        elif task == flows.ENGINE:
            self.mark("message", ENGINE_MESSAGES.get(ident, f"Engine message {ident:#04x}"))
        elif task == flows.POWER:
            self.mark("message", "Pairing timer expired" if ident == 3 else f"Power message {ident:#04x}")
        super().call(address)
        self._note_state()

    def _note_state(self):
        pointer = self.read(0x588)
        state = self.read(pointer + 1) if pointer else None
        if state != self.last_state:
            if self.last_state is not None:
                names = {1: "Normal", 2: "Pairing"}
                self.mark("state", f"Connection state: {names.get(state, state)}")
            self.last_state = state

    def _call(self, target, following, stack):
        if target in self.stubs:
            if target == dp.BATTERY:
                self.mark("output", "Battery announcement")
            elif target == dp.PLAY_SOUND:
                self.mark("output", SOUNDS.get(self.AL, f"Sound {self.AL}"))
            elif target == dp.LEAVE_CONNECTIONS:
                self.mark("output", "Leave connections")
        return super()._call(target, following, stack)


def scenario(words, presses, until, power_handler=False, state=None):
    flows.Machine = Tracer
    m = flows.machine(words)
    if power_handler:
        m.handlers[flows.POWER] = 0x10831
    m._note_state()
    if state is not None:
        m.write(m.read(0x588) + 1, state)
    error = None
    try:
        for at, held in presses:
            m.run_until(at)
            m.now = at
            m.mark("press", "Button pressed")
            down = m.alloc(1)
            m.write(down, flows.BUTTON_POWER)
            m.send(flows.ENGINE, 0, down, 0)
            m.run_until(at + held)
            m.now = at + held
            m.mark("press", "Button released")
            up = m.alloc(1)
            m.write(up, 0)
            m.send(flows.ENGINE, 0, up, 0)
            m.run_until(at + held)
        m.run_until(until)
    except Unmodelled as err:
        error = str(err)
    m.mark("end", f"End, {until / 1000:g} s" if not error else "Stopped: not modelled")
    return {"pcs": m.trace, "marks": m.marks, "error": error, "end": until}


def classify(words, covered_unused, patch_code, hook, table) -> list[int]:
    kinds = [KINDS.index("code")] * len(words)
    for i in range(dp.HEADER_WORDS if hasattr(dp, "HEADER_WORDS") else 0x18):
        kinds[i] = KINDS.index("header")
    for i in range(CODE_LIMIT, len(words)):
        kinds[i] = KINDS.index("constants")
    i = CODE_LIMIT
    while i < len(words):
        j = i
        if 32 <= words[i] < 127:
            while j < len(words) and 32 <= words[j] < 127:
                j += 1
            if j - i >= MIN_RUN:
                for k in range(i, j):
                    kinds[k] = KINDS.index("text")
            i = max(j, i + 1)
        elif words[i] in (0, 0xFFFF):
            while j < len(words) and words[j] in (0, 0xFFFF):
                j += 1
            if j - i >= MIN_RUN:
                for k in range(i, j):
                    kinds[k] = KINDS.index("zeros")
            i = max(j, i + 1)
        else:
            i += 1
    for start, end in covered_unused.items():
        for k in range(start, end):
            kinds[k] = KINDS.index("unused")
    for k in table:
        kinds[k] = KINDS.index("table")
    for k in patch_code:
        kinds[k] = KINDS.index("patch")
    for k in hook:
        kinds[k] = KINDS.index("hook")
    return kinds


def run_length(values: list[int]) -> list[int]:
    out = []
    i = 0
    while i < len(values):
        j = i
        while j < len(values) and values[j] == values[i]:
            j += 1
        out += [values[i], j - i]
        i = j
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("firmware", type=Path, nargs="?", default=ROOT / "firmware" / "HDX-2916_V0452_SPLC_with_stack.bin")
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "work" / "viz" / "firmware.html")
    parser.add_argument("--open", action="store_true", help="open the page in your browser")
    parser.add_argument("--artifact", action="store_true", help="leave out the document header, for publishing")
    args = parser.parse_args()
    image = args.firmware.read_bytes()
    layout = dp._layout(image)
    with tempfile.TemporaryDirectory() as folder:
        app = dp.app_from_image(image, Path(folder) / "vm.app")
    words = list(app.words)
    edits, _ = dp.build_edits(app, layout.covered)
    data_edits = dfu_sign.pairing_edits(image, HOLD_MS)
    built = dfu_sign.patch(image, {**edits, **data_edits})
    with tempfile.TemporaryDirectory() as folder:
        patched = list(dp.app_from_image(built, Path(folder) / "vm.app").words)
    changed = {i for i, (a, b) in enumerate(zip(words, patched)) if a != b}
    hook = set(dp.HOOK_WORDS) & changed
    patch_code = (set(edits) - set(dp.HOOK_WORDS)) & changed
    table = changed - hook - patch_code
    unused = dp.unreferenced_functions(app)
    kinds = classify(words, unused, patch_code, hook, table)

    phase = (layout.start - dfu_sign.FS_HASH_START) % dfu_sign.BLOCK_WORDS
    covered = [layout.covered(i) for i in range(len(words))]
    if any(covered[i] != ((i + phase) % 32 < dfu_sign.COVERED_PER_BLOCK) for i in range(0, len(words), 97)):
        raise SystemExit("the signature pattern doesn't match the expected phase")

    runs = [
        {"id": "double", "name": "Double click", "image": "Patched",
         "note": "The first click arms the timer and announces the battery. The second finds it running, drops the connections, and posts a click for three seconds later. That click starts pairing.",
         **scenario(patched, [(0, 120), (320, 120)], 4500)},
        {"id": "single", "name": "One click", "image": "Patched",
         "note": "A single click announces the battery. The private timer runs out with nothing to do.",
         **scenario(patched, [(0, 120)], 1200)},
        {"id": "stock", "name": "Two clicks", "image": "Sony's",
         "note": "Sony's firmware with no patch: two clicks are simply two battery announcements.",
         **scenario(words, [(0, 120), (320, 120)], 1500)},
        {"id": "hold", "name": "Hold 5 s", "image": "Patched",
         "note": "Timers fire at 1.5 s and 4 s from the moved table. This run starts in the on state, and most of what those events trigger is stubbed or not modelled.",
         **scenario(patched, [(0, 5000)], 6000)},
        {"id": "timeout", "name": "Pairing timeout", "image": "Patched",
         "note": "A double click starts pairing. Five minutes later the firmware's own timer returns to normal. The wait is skipped.",
         **scenario(patched, [(0, 120), (320, 120)], 304500, power_handler=True)},
    ]
    free_unsigned = sum(1 for i, c in enumerate(covered) if not c)
    counts = {k: 0 for k in KINDS}
    free_counts = {k: 0 for k in KINDS}
    for i, kind in enumerate(kinds):
        counts[KINDS[kind]] += 1
        if not covered[i]:
            free_counts[KINDS[kind]] += 1
    unused_words = sum(1 for k in range(len(words)) if kinds[k] == KINDS.index("unused"))
    data = {
        "total": len(words), "codeLimit": CODE_LIMIT, "phase": phase, "kinds": KINDS,
        "map": run_length(kinds), "functions": {hex(k): v for k, v in FUNCTIONS.items()},
        "starts": sorted({x[0] for x in app.ins if x[2].startswith("sys 0x") and x[2].endswith("0b")} | set(FUNCTIONS)),
        "unused": [[k, v] for k, v in sorted(unused.items())],
        "counts": counts, "freeCounts": free_counts, "unsigned": free_unsigned,
        "unusedFunctions": len(unused), "unusedWords": unused_words,
        "patchWords": len(patch_code) + len(hook), "tableWords": len(table), "runs": runs,
        "findings": build_findings(app),
    }
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", json.dumps(data, separators=(",", ":")))
    if not args.artifact:
        html = '<!doctype html>\n<html lang="en"><meta charset="utf-8">\n' \
               '<meta name="viewport" content="width=device-width, initial-scale=1">\n' + html
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote {args.output}, {len(html) // 1024} KB")
    for run in runs:
        print(f"  {run['id']:8} {len(run['pcs']):7} instructions, {len(set(run['pcs'])):6} words, "
              f"{len(run['marks']):3} marks, error: {run['error']}")
    if args.open:
        webbrowser.open(args.output.resolve().as_uri())
    print(f"  unsigned {free_unsigned} of {len(words)} = {free_unsigned * 100 / len(words):.1f}%, {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
