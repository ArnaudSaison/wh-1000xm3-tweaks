#!/usr/bin/env python3
"""Change the button timings of firmware 4.5.2, with checks, for the web UI and the command line.

    uv run tools/button_timing.py firmware.bin out.bin --short-ms 1000 --long-ms 4000
    uv run tools/button_timing.py firmware.bin out.bin --short-ms 1000 --long-ms 3000 --double-press

Only values with unsigned words, or a table that can move to unsigned words, can change.
`check` applies rules to the values. `rehearse` then presses the buttons in the emulator,
on the firmware's own button engine in the built file. Neither can show how it feels, or
what the radio does. See docs/firmware-analysis.md.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tempfile  # noqa: E402

import dfu_sign  # noqa: E402
import double_press as patch  # noqa: E402
from double_press import _vm_words  # noqa: E402
from xap_machine import Machine  # noqa: E402

ENGINE, APP = 0x8E47, 0x7DCA
ENGINE_HANDLER = 0x1153C
DESCRIPTORS = 0x3776C
# Pairs of duration words for (hold, release) entries that are unsigned in place.
INPUT_2_WORDS = (0x3772D, 0x37730)
INPUTS_5_6_WORDS = (0x37733, 0x37736, 0x3774B, 0x3774E)
REPEAT_START_MS = 950   # signed on input 5, so it stays
MIN_GAP_MS = 1000
COMFORT_GAP_MS = 2000
COMFORT_SHORT_MS = 800
COMFORT_LONG_MS = 3000

CONTROLS = [
    {"id": "short_ms", "group": "Power button", "label": "Short hold",
     "help": "Separates a click from a hold. On: starts powering off. Off: turns on, when released.",
     "stock": 1500, "min": 500, "max": 5000, "step": 100, "known": True},
    {"id": "long_ms", "group": "Power button", "label": "Long hold",
     "help": "Off: enters pairing. On, still holding after power-off started: comes back on, in pairing by the code.",
     "stock": 7000, "min": 2000, "max": 15000, "step": 100, "known": True},
    {"id": "input2_ms", "group": "Not identified yet", "label": "Input 2 hold",
     "help": "Events 0x0B and 0x0C. Which control this is hasn't been established.",
     "stock": 260, "min": 150, "max": 1000, "step": 10, "known": False},
    {"id": "inputs56_ms", "group": "Not identified yet", "label": "Inputs 5 and 6, first step",
     "help": "Events 0x35/0x36 and 0x3B/0x3C. Possibly volume (inferred). Stays below the 950 ms repeat start.",
     "stock": 800, "min": 300, "max": 900, "step": 50, "known": False},
]
LOCKED = [
    {"what": "Input 0, 15 s", "why": "Could be moved, but left alone: unidentified, and it could be a reset."},
    {"what": "Inputs 5 and 6, repeat 950 ms, then every 150 ms", "why": "Signed on input 5. Changing only 6 would make the pair differ."},
    {"what": "Inputs 5 and 6, 1.5 s, 2 s and 3 s", "why": "Input 5's table is too long to move: 24 words, and unsigned runs stop at 15."},
    {"what": "Input 7, 260 ms, and input 8, 2 s", "why": "Signed, and their table pointers are signed too."},
    {"what": "1.5 s start delay with no device stored", "why": "A separate value in the code. Part of it is signed."},
]
FEATURES = [
    {"id": "double_press", "label": "Double press for pairing",
     "help": "While on, two quick clicks of the power button enter pairing. One click still announces the "
             "battery. Pairing starts three seconds after the second click, once the phone has been dropped. "
             "This adds code, where the sliders only change numbers."},
]
STOCK = {c["id"]: c["stock"] for c in CONTROLS}
BY_ID = {c["id"]: c for c in CONTROLS}


def normalise(values: dict) -> dict[str, int]:
    """Fill in stock values and require whole numbers. Unknown names are refused."""
    unknown = set(values) - set(STOCK)
    if unknown:
        raise dfu_sign.SignError(f"unknown timing: {sorted(unknown)[0]}")
    out = dict(STOCK)
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise dfu_sign.SignError(f"{BY_ID[name]['label']} must be a whole number of milliseconds")
        out[name] = int(value)
    return out


def check(values: dict, double_press: bool = False) -> list[dict]:
    """Rules for these values. `error` stops a build, `warn` is a choice the owner can make."""
    v = normalise(values)
    found = []

    def note(level, text):
        found.append({"level": level, "text": text})

    for control in CONTROLS:
        value = v[control["id"]]
        if not control["min"] <= value <= control["max"]:
            note("error", f"{control['label']} must be {control['min']} to {control['max']} ms.")
    short, long = v["short_ms"], v["long_ms"]
    gap = long - short
    if gap < MIN_GAP_MS:
        note("error", f"The long hold must be at least {MIN_GAP_MS / 1000:g} s after the short hold, "
                      "or there's no time to release for a plain power-off or power-on.")
    elif gap < COMFORT_GAP_MS:
        note("warn", f"Only {gap / 1000:g} s to release between the two holds. Holding slightly too long "
                     "cancels a power-off, or turns a power-on into pairing.")
    if short < COMFORT_SHORT_MS:
        note("warn", f"A {short / 1000:g} s short hold: a slow click, or a knock in a bag, could power off. "
                     "Below this, the battery click also gets harder to do.")
    if long < COMFORT_LONG_MS:
        note("warn", f"A {long / 1000:g} s long hold hasn't been tried. The shortest tried on hardware is 4 s.")
    if short != STOCK["short_ms"]:
        note("info", "A changed short hold hasn't been tried on a real headset. It goes in the same moved "
                     "table as the long hold, which has.")
    for name in ("input2_ms", "inputs56_ms"):
        if v[name] != STOCK[name]:
            note("warn", f"{BY_ID[name]['label']}: nobody knows yet what this control does, so what changes "
                         "can only be found by trying it.")
    if double_press:
        note("warn", "A first version of the double press works on a real headset. This one adds the wait "
                     "before the pairing sound, and has not run on one. It replaces parts of seven functions "
                     "that appear unused, three more than the first. If one is used at start-up, the headset "
                     "may not boot.")
        note("info", f"The two clicks must end within {double_press_window() / 1000:g} s of each other, "
                     f"and each must be shorter than the {short / 1000:g} s short hold.")
    elif v == STOCK:
        note("error", "These are Sony's values. There's nothing to build.")
    return found


def double_press_window() -> int:
    return patch.DEFAULT_WINDOW_MS


def double_press_edits(image: bytes) -> dict[int, int]:
    """The double-press patch for this file, or SignError if the file isn't the firmware it was written for."""
    with tempfile.TemporaryDirectory() as folder:
        app = patch.app_from_image(image, Path(folder) / "vm.app")
    try:
        edits, _ = patch.build_edits(app, patch._layout(image).covered)
    except patch.PatchError as err:
        raise dfu_sign.SignError(f"double press: {err}") from err
    return edits


def edits_for(image: bytes, values: dict, double_press: bool = False) -> dict[int, int]:
    v = normalise(values)
    problems = [c["text"] for c in check(v, double_press) if c["level"] == "error"]
    if problems:
        raise dfu_sign.SignError(problems[0])
    vm = _vm_words(image)
    if [vm[w] for w in INPUT_2_WORDS] != [STOCK["input2_ms"]] * 2 or \
            [vm[w] for w in INPUTS_5_6_WORDS] != [STOCK["inputs56_ms"]] * 4:
        raise dfu_sign.SignError("this is not the expected firmware: the button table differs")
    edits: dict[int, int] = {}
    if v["short_ms"] != STOCK["short_ms"] or v["long_ms"] != STOCK["long_ms"]:
        edits.update(dfu_sign.pairing_edits(image, v["long_ms"], v["short_ms"]))
    if v["input2_ms"] != STOCK["input2_ms"]:
        edits.update({w: v["input2_ms"] for w in INPUT_2_WORDS})
    if v["inputs56_ms"] != STOCK["inputs56_ms"]:
        edits.update({w: v["inputs56_ms"] for w in INPUTS_5_6_WORDS})
    if double_press:
        code = double_press_edits(image)
        if set(code) & set(edits):
            raise dfu_sign.SignError("the double-press code and the timing changes want the same words")
        edits.update(code)
    return edits


def build(image: bytes, values: dict, double_press: bool = False) -> bytes:
    return dfu_sign.patch(image, edits_for(image, values, double_press))


def _machine(words: list[int]) -> Machine:
    m = Machine(words, {ENGINE: ENGINE_HANDLER})
    for pointer, size in {0x54E: 0x20, 0x552: 0x60, 0x586: 0x20}.items():
        m.write(pointer, m.alloc(size))
    engine = m.read(0x586)
    m.write(engine + 0, APP)      # where the engine sends events
    m.write(engine + 3, 0xFFFF)   # no input down
    m.write(engine + 4, 0xFFFF)   # no pending click
    config = m.read(0x552) + 7    # the firmware's default input map: bit n is input n
    for i in range(0x40):
        m.write(config + i, m.read(0x801A + i))
    m.write(config + 0x34, 0)     # no combination remaps
    m.write(config + 0x38, 0)
    m.stubs.update({0x11705: lambda s: None})
    return m


def press(words: list[int], number: int, held_ms: int) -> list[tuple[int, int]]:
    """Hold one input through the real engine. Returns the events sent, as (time, event)."""
    m = _machine(words)
    for at, mask in ((0, 1 << number), (held_ms, 0)):
        m.run_until(at)
        payload = m.alloc(1)
        m.write(payload, mask)
        m.send(ENGINE, 0, payload, 0)
        m.run_until(at)
    m.run_until(held_ms + 500)
    return [(when, event) for when, task, event in m.delivered if task == APP]


def rehearse(image: bytes, values: dict, double_press: bool = False) -> list[dict]:
    """Press the changed inputs in the emulator, on the built file. Each row must match.

    The double-press rows run with or without the option: without it, they check that a
    double click is still two battery announcements, so a file can't carry the patch unasked.
    """
    v = normalise(values)
    words = _vm_words(image)
    short, long = v["short_ms"], v["long_ms"]
    power = [
        (f"Click, {short - 100} ms", short - 100, [(0, 0x11), (short - 100, 0x12)]),
        (f"Just past the short hold, {short + 100} ms", short + 100, [(0, 0x11), (short, 0x13), (short + 100, 0x14)]),
        (f"Just before the long hold, {long - 100} ms", long - 100, [(0, 0x11), (short, 0x13), (long - 100, 0x14)]),
        (f"Past the long hold, {long + 100} ms", long + 100, [(0, 0x11), (short, 0x13), (long, 0x15), (long + 100, 0x16)]),
    ]
    rows = []
    for label, held, expected in power:
        got = press(words, 1, held)
        rows.append({"label": f"Power button: {label}", "ok": got == expected,
                     "detail": " ".join(f"{e:#04x}@{t}" for t, e in got)})
    for number, name, event, after in ((2, "input2_ms", 0x0B, 0x0C), (5, "inputs56_ms", 0x35, 0x36), (6, "inputs56_ms", 0x3B, 0x3C)):
        ms = v[name]
        got = press(words, number, ms + 20)
        ok = (ms, event) in got and (ms + 20, after) in got and all(t >= ms or t == 0 for t, _ in got)
        rows.append({"label": f"Input {number}: fires at {ms} ms", "ok": ok,
                     "detail": " ".join(f"{e:#04x}@{t}" for t, e in got)})
    clicks = patch.rehearse(words, short_ms=short)
    if double_press:
        rows += clicks
    else:
        row = clicks[0]
        rows.append({"label": "Double click without the option: battery twice",
                     "ok": row["detail"] == "battery x2, pairing sound x0, state 1", "detail": row["detail"]})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("output", type=Path)
    for control in CONTROLS:
        parser.add_argument("--" + control["id"].replace("_", "-"), type=int, default=control["stock"], dest=control["id"])
    parser.add_argument("--double-press", action="store_true", help="add double press for pairing")
    args = parser.parse_args()
    values = {c["id"]: getattr(args, c["id"]) for c in CONTROLS}
    for row in check(values, args.double_press):
        print(f"{row['level']}: {row['text']}")
    try:
        output = build(args.firmware.read_bytes(), values, args.double_press)
    except dfu_sign.SignError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    rows = rehearse(output, values, args.double_press)
    for row in rows:
        print(f"{'ok ' if row['ok'] else 'BAD'} {row['label']}: {row['detail']}")
    if not all(row["ok"] for row in rows):
        print("refused: a rehearsal didn't match", file=sys.stderr)
        return 1
    args.output.write_bytes(output)
    print(f"wrote {args.output}, sha1 {hashlib.sha1(output).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
