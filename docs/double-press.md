# Double press for pairing

A quick double press of the power button, while the headset is on, enters pairing mode. A single press still announces the battery. Built by `tools/double_press.py`, on firmware 4.5.2. **A first version works on the headset. The current one, which waits before the pairing sound, has not run on it.**

Labels are as in [`firmware-analysis.md`](firmware-analysis.md): **Read** means seen in the disassembly, **Inferred** means a reasoned interpretation, and **Emulated** means run in `tools/xap_machine.py`.

## Correction: the firmware has no double-press detector

The entries with bit 15 set in the timing table were labelled "double press". **They are auto-repeat (read).** In `0x1170C`, such an entry sends its event, then reschedules itself. In `0x114FD`, the first delay is the low 15 bits of the first word, and later delays are the second word divided by 2. So inputs 5 and 6 mean: after 950 ms, repeat every 150 ms. Events `0x35` and `0x3B` both go to `0x8DD1`. This looks like held volume change (inferred).

The remap slots at offsets `0x34` and `0x38` of the input map compare against a whole button mask, so they are probably button combinations (inferred), not double presses. Adding a table entry could never have given a double press.

## How a press reaches the application (read)

| Step | Where | What happens |
|---|---|---|
| Input changes | `0x1153C`, message 0 | `0x11457` maps a button mask to an input number, through a table with 4 words per entry |
| Press | `0x1186D` | Cancels old timers, sends down and click events from `0x8DBA`, and starts hold timers from `0x8E29` |
| Hold timer fires | `0x1170C` | Sends the event, and cancels the pending click, so a hold never counts as a click |
| Release | `0x1186D` | Sends the pending click, or the release event of the longest hold reached |

Each input has two entries in `0x8DBA`. For input 1 they are `0x11` on press, and `0x12` on release if no hold timer fired. So **a short click on the power button already reaches the application as event `0x12`**.

In the normal on state, `0x9299` handles `0x12` by calling `0x89AB(0, 0)` at `0x92D1`, which starts the battery announcement. The owner confirms that a short click announces the battery.

## How pairing starts (read, meaning inferred)

Every pairing start ends in **`0x11D84(1)`**: it sets connection state 2, starts the 300 s timer (power task message 3), and makes the headset discoverable through `0x8AB1(1)`. When the timer fires, `0x12913` returns to state 1. There are two ways in.

**From off.** The long hold asks for power mode 2 through `0x9807`. That request reaches `0x11CE3`, which plays sound 8 when the mode changes to 2, and stores the mode. The start function `0x10B0C` then switches on the mode, and mode 2 runs `0x12F4E` then `0x11D84(1)`.

**From on.** This path was missed before. Holding the button while on sends event `0x13` at 1.5 s, which starts the power-off (`0x11E01(3, 2)`: state 3, stop reconnecting, power-off sound). If the button is still held when event `0x15` arrives, `0x9429` calls `0x9A49`, which sends message `0x12` to the power task. Its handler `0x10BDD` cancels the power-off timers, plays sound 8, sets state 2, and three seconds later message `0x13` runs `0x10C1D`, which is just `0x11D84(1)`. This fits what the owner saw: "still holding at 7 s turns it back on". Whether that ends in pairing mode has not been checked on the headset, and it is a free test: see below.

So `0x11D84(1)` is already reached while the chip has been on all along, with the power mode word still 1. The mode word is only read by the boot-time start function and by mode requests (read: `0x10906`, `0x10B1D`, `0x9417`, `0x11CE3`).

## The patch

```
event 0x12, normal on state
  │
  ▼
0x92D0: call the new code        (was: call 0x89AB; the two words of that call are all that change here)
  │
  ├─ not state 1 ──────────────────────────► battery announcement, as before
  │
  ▼
cancel the marker timer
  │
  ├─ one was pending: the posted click ─► sound 8, 0x11D84(1)
  │
  ▼
cancel the click timer
  │
  ├─ nothing was pending: first click ──► start click timer (500 ms), battery announcement
  │
  └─ one was pending: second click ─────► stop reconnecting (0x12F4E), leave connections (0x12040),
                                          start marker timer (10 s),
                                          post event 0x12 to the application, 3 s from now
```

**Why two steps.** The first version did everything on the second click, and on the headset it said "Bluetooth disconnected" instead of "Bluetooth pairing". The cause (read): when the link drops in state 1 or 2, `0x12FC0` plays sound 12, and in the normal on state a new sound cuts off the one playing (`0x58F` is 1 after any sound has finished). So sound 8 cut off the battery announcement, and a moment later sound 12 cut off sound 8. Now nothing asks for a sound until three seconds after the links are dropped. Sony's own `0x10BDD` waits the same three seconds between its sound and `0x11D84(1)`.

**The second step arrives as a click.** The code can only run when the application handles event `0x12`, so it posts that event to the application task itself, with `0x10A81`. Sony's code posts the same event to the same task the same way at `0x9863`. A marker timer tells the posted click from a real one. If the state is no longer 1 when it arrives, for instance after a power-off, nothing happens and the marker runs out.

**The timers hold the state.** They are messages `0x7F` and `0x7E` to the button engine's task, whose handler ignores unknown ids (read: `0x1153C` compares the id with 0 to 5, `0x8023` and `0x8025`, and otherwise returns). That needs no RAM, no new event and no boot code.

**Each step is something Sony's code does while on (read).**

| Step | Sony's own use in the on state |
|---|---|
| Sound 8, then `0x11D84(1)` | `0x10BDD`, then `0x10C1D` |
| `0x12F4E`, then `0x12040` | Same order in the power-off path (`0x11E01`, later `0x11E5F`) and in `0x14C32` |
| `0x12040` from state 1 | `0x11DF0` and `0x92FF` call it straight from an on state |
| Posting event `0x12` to the application | `0x9863` |

The power mode word is left alone, as in Sony's from-on path. An earlier build set it to 2, and that write is gone.

**Risk profile.** The new code only runs on a short click in state 1. Every other path, including boot and the update, is unchanged, unless one of the replaced functions is actually in use. So a mistake in the pairing steps shows up after a double click, and a power cycle and a reflash undo it. Only a wrong "unused function" claim could stop the headset starting.

### Where the code lives

| Constraint | Consequence |
|---|---|
| Only uncovered words can change | Fragments of at most 15 words, joined by jumps |
| The `vm.app` header declares a code size of `0x35D2B` words after a `0x18`-word header | Code stays inside the code segment, in case that limit is enforced |
| Sony's code uses far calls but no far `bra` | All fragments are within 32K of each other |
| The chip may differ from the emulator on forms nobody has seen run | Every instruction uses an encoding form found in Sony's own code. The build refuses otherwise (`foreign_shapes`) |

The code replaces unsigned words of functions that nothing refers to: no call, no branch in, no fall-through from the word before, and no pointer to them in any form in the code or constants. `unreferenced_functions` repeats this check at every build. **That is strong evidence, not proof.** If one of them is used in a way this check can't see, that feature would crash.

What backs the claim:

- **The pointer form is known.** Task structures store handlers as a high and low word, minus the header: `0x1524` with high word 1 is `0x1153C`. Of the 210 functions with no direct call or branch, about 115 are found through exactly that form, in the constants or as an immediate. The pointer values of the seven functions used here occur nowhere in the image, as any word at all.
- **They look like unused library calls.** `0x34EE` is one of a row of near-identical functions, differing by a constant (`0x4000`, `0x5000`, `0x6000`), whose neighbours each have one or two callers. `0x3666` undoes what `0x364C` does, and neither is called. `0x3968` is a getter beside a twin that is called.
- **Four of them are proven harmless to replace.** `0x34EE`, `0x3666`, `0x3968` and `0x5B20` were replaced in the first version, and the headset boots, connects and plays. The current code is longer and also uses `0x5ACC`, `0x6109` and `0x7963`. `0x5ACC` is the twin of `0x5B20`, on the same object.
- **Each is one plain function.** One frame entry, one matching leave as the last word, and no `rts` inside, so no second function hides in the range.

## Emulated

`tests/test_double_press.py` runs real firmware code, from an input message through the engine and the application handler, in stock and patched images.

| Scenario | Stock | Patched |
|---|---|---|
| One click | Battery | Battery, and the timer expires |
| Two clicks, 300 ms apart | Battery twice | Battery, connections left, and 3 s later the pairing sound and state 2 |
| A third click during the 3 s | | Pairing at once |
| State changes during the 3 s | | No pairing, the marker runs out, clicks are normal again |
| Two clicks, 800 ms apart | | Battery twice |
| Hold 2 s | Hold event at 1.5 s, no battery | The next click starts afresh |
| Click, then hold | | No pairing |
| State 9 | Battery twice | Battery twice, unchanged |
| Any link state in words 3 and 4 of `0x588` | Battery twice | Battery, then pairing |
| After pairing starts | | State 2 until 300 s, then back to normal, and it works again |

These tests caught two real bugs: a call that jumped past its frame entry, and a dependence on a far `bra`.

**X is the link register (read, now emulated).** `rts` is the word `0x00E2`, which decodes as `bra @(0,X)`. Leaf functions never touch X, and every function that uses X opens a frame first, even a one-line getter. The emulator now puts the return address in X at each call and refuses an `rts` if X changed. All of Sony's emulated flows still pass under that rule, and so does the patch, which reloads X before each use.

## What the emulator can't show

- **Models:** firmware calls are models. RAM starts at zero rather than after a real boot, and the input map is the firmware's default table rather than one built from settings.
- **Replaced functions:** sound playback, LED updates, audio mode switching (`0x11175`), discoverability (`0x8AB1`) and `0x12040` are stubs. So no radio behaviour has been tested.
- **A connected phone:** whether `0x12040` drops the link cleanly, and whether the phone then reconnects during pairing, can only be seen on hardware.

## Sound (read, and heard)

`0x14D56` either cuts off the sound that is playing or keeps the new one in a one-slot queue at `0x590`, depending on the flag at `0x58F`. That flag is set whenever a sound finishes and only cleared by `0x10BDD`, so in the normal on state a new sound cuts off the old one. The earlier note here, that sound 8 could not be lost, was wrong: it was cut off by the disconnect announcement.

Expected now, with a phone connected: the battery announcement, cut short by "Bluetooth disconnected", then "Bluetooth pairing" about three seconds after the second click. Without a phone: the battery announcement, then "Bluetooth pairing".

## With a phone connected (read, meaning inferred)

The click handler sends states 1 and 9 to the hooked call, and states 5 to 7 to a second battery call at `0x92EB`, which is not hooked. So in states 5 to 7 and 9 the patch changes nothing.

A connection most likely leaves the state word at 1. The connect handler `0x12F54` plays "connected" only in states 1 and 2 and doesn't write the state, the disconnect handler `0x12FC0` has the same test, and none of the 29 calls to the setter `0x1259E` sits in either handler. Link and call state live in words 3 and 4 of `0x588`. The whole connect path has not been traced, so this is not proven. If double click only works unconnected, this is still the first place to look.

## From off

**This only works while the headset is on.** From off, the firmware can't see a press until it has booted, which appears to take about 2 s: a 4 s hold measured about 6 s. A quick double press would be over before any code runs (inferred). Changing that would need hardware behaviour we don't control.

## On the headset

**2026-11-10, first version** (`doublepress-6e7be57d`, with holds of 1 s and 3 s): it boots and works, and a double click enters pairing. It said "Bluetooth disconnected" instead of "Bluetooth pairing". That led to the two-step version above, which has not been flashed.

## Checks on the headset

**Before flashing, on the current firmware. Both are free.**

1. With the phone connected, click once. A battery announcement means the click reaches a battery call while connected.
2. With the phone connected, hold the button through the power-off announcement until the pairing hold time. If it comes back in pairing mode (pairing voice, double blue flash, visible to a new device), then Sony's from-on path works, and `0x11D84(1)` is proven on this headset after links were dropped while on. That is the same last step the patch takes.

**Building it.** Tick "Double press for pairing" in the web UI's firmware section, which builds the patch with the hold times on the sliders, or run `tools/button_timing.py … --double-press`. A file named `timing-…` in the UI has no double press; one named `doublepress-…` does. `work/fw/double_press_v2_4s.bin` is the same patch with a 4 s hold.

**After flashing a double-press file, in this order.** Stop at the first surprise, power cycle, and flash the previous file back.

1. It boots, the phone reconnects, music plays. This is the test of the "unused function" claim for everything a normal session touches.
2. One click, not connected: battery announcement, nothing else.
3. Hold to power off, then on again: unchanged.
4. Double click, not connected: pairing voice and double flash. Pair a new device.
5. Double click with the phone connected: the phone drops, then pairing as above.
6. Leave pairing by powering off with a hold. Power on: the phone reconnects.
7. Optional: let pairing time out after 5 minutes, and check a click announces the battery again.

## Inherited tools

`tools/xap_inject.py` is reused for linking. `trampoline()` isn't used here, because it saves registers to `0xC700`, an address not shown to be writable RAM. `free_slots()` isn't used either, because uncovered `0x0000` and `0xFFFF` words can be real data, and any outside the code segment may not be executable.

The disassembler prints `st` with immediate mode as `print`. These are conditional branches after `cmp`, signed, unlike `bcc` and `bcs`. By use, the four registers mean greater than, greater or equal, less than, and less or equal (inferred). The patch uses none of them.
