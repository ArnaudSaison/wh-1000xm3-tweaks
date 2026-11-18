# Emulating the button-timing engine

`tools/xap_emulator.py` is a tiny XAP interpreter that runs the *real* bytecode of
the WH-1000XM3 button-timing engine, so a timing change can be checked on the PC
before anything is flashed. It is **not** a full-chip emulator: it executes two
functions in `vm.app` and stubs every firmware call.

## What it runs

| Address | Role |
|---|---|
| `0x11523` | per-input dispatch: reads the timing descriptor for an input number and walks its table |
| `0x114FD` | leaf: for one table entry, posts a delayed message whose delay is the entry's duration (firmware call 16) |

Firmware calls are modelled: call 9 (alloc) hands back scratch RAM; call 16 (post
message) records `(delay, event)` instead of sending. Running the dispatch for an
input therefore yields the exact list of `(delay_ms, event)` the firmware would
schedule when that button is first pressed — which is the data the pairing patch
edits. Only the opcodes those two functions use are implemented; anything else
raises `Unmodelled`, so the emulator fails loudly rather than guessing.

## Demonstrating the pairing patch

```
uv run tools/xap_emulator.py <vm.app> --ms 4000 --hold 5000
```

```
input 1, as shipped (Sony 4.5.2):
     1500 ms  event 0x13  power on / short-hold action
     1500 ms  event 0x14  short-hold release
     7000 ms  event 0x15  ENTER PAIRING (long hold)
     7000 ms  event 0x16  long-hold release

input 1, after the pairing patch (--ms 4000):
     1500 ms  event 0x13  power on / short-hold action
     1500 ms  event 0x14  short-hold release
     4000 ms  event 0x15  ENTER PAIRING (long hold)
     4000 ms  event 0x16  long-hold release

holding 5000 ms, as shipped: does NOT reach pairing (events ['0x13', '0x14'])
holding 5000 ms, patched: enters pairing (events ['0x13', '0x14', '0x15', '0x16'])
```

The emulator never looks at the patch directly. `apply_pairing_patch` makes the
same uncovered-word edits the flashed firmware had (relocate input 1's table into a
debug string, repoint its descriptor); the interpreter then follows the repointed
pointer to the relocated table on its own and the pairing hold comes out at 4000 ms.
This reproduces, offline, the behaviour measured on the real headset.

## How far to trust it

- It validates **our logic** — that a table/pointer edit produces the intended
  schedule, and that an injected change doesn't crash this code path.
- It does **not** validate the hardware's undocumented firmware calls. The stubbed
  traps are models; real behaviour still needs the headset.

## Why this matters for the next step

The standing goal is to make pairing a **double-press of the power button** instead
of the long hold. That is a logic change, not a timing edit, so it needs more than a
table tweak. This emulator is the harness for developing that: run the dispatch for
the power button, add the double-press handling, and confirm the event flow here
before ever writing it to flash.

## Tests

`tests/test_xap_emulator.py` cross-checks the interpreter against an independent read
of the timing table for several inputs, so a bug in the interpreter can't quietly
pass. The tests skip automatically where `vm.app` isn't present (it is git-ignored).
