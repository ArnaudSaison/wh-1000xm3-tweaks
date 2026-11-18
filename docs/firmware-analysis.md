# Firmware analysis: updates, signatures, buttons, LEDs

Static analysis of firmware 4.5.2, using `tools/xapanalyze.py` and `tools/dfu_sign.py`. Addresses are 16-bit word offsets. Two changes have been tried on hardware: the announcements, and a 4 s pairing hold.

Labels: **Proven** means checked against Sony's real data. **Read** means seen directly in the disassembly. **Inferred** means a reasoned interpretation. Firmware call names are always inferred, because the numbers are undocumented.

## Signatures

### The scheme (proven)

The check lives in the stack firmware (`stack0.bin`, the same XAP instruction set as `vm.app`, little-endian). `tools/dfu_sign.py verify` reimplements it, and it reproduces Sony's real signatures for the stack settings, app settings and filesystem, in both the global and SPLC 4.5.2 files.

| Part | Finding |
|---|---|
| Algorithm | RSA-1024, **e = 3, no padding**: `signature³ mod N` must equal the digest. Code at `0x5E934` |
| Key | PS keys 350-354, loaded at `0x5E949` |
| Digest | 8 MD5 chains side by side, 1024 bits, top bit cleared. Code at `0x5E6CD`-`0x5E933` |
| Compare | 64 words, memcmp-style, at `0x406F9` |

### The flaw: about 47% of signed data is not covered (proven)

The digest fetches MD5's 32-bit inputs with `word[g] | word[g+1] << 16`, using 16-bit word addressing, and `g` only runs 0 to 15. Each 32-word block therefore only uses **words 0-16. Words 17-31 do not affect the digest.**

Proof: the model with this behaviour matches Sony's signatures. Standard indexing does not. Changing word 17 or 31 of the app settings section leaves the signature valid, and changing word 5, 16 or 32 breaks it.

Hashed ranges, counted from the start of hashing:

| Section | Hashed words |
|---|---|
| Stack and app settings | Start of the section to just before the signature |
| Filesystem | Word 68 to the last word minus one |

So **uncovered words can be changed while Sony's own signature stays valid, without any private key.** A real headset has accepted and booted a file changed this way. Forging a signature remains unrealistic and is not needed.

### Missing key means no check (read)

In `0x406F9`, if reading the key fails, the result is set to 1, and the caller treats that as success (`0x64`). A mismatch gives `0x66`. So with PS keys 350-354 erased through SPI, unsigned files would pass this check. This is not proven on hardware.

### Update checks in `vm.app`

**Validation is compiled out (read).** The hooks `0x32F3F` and `0x32F41` are `ld AL, #1 ; rts`. They are called for every block and the footer, so errors `0x38`-`0x3D` can never fire. Sony's packages have an all-zero footer, consistent with this.

The parser (`0x33280`) still checks structure:

| Stage | Check | Error |
|---|---|---|
| Any chunk | Header at least 12 bytes | `0x31` |
| `APPUHDR2` | At least 14 bytes | `0x32` |
| | 8-byte variant compare | `0x13` |
| | Compatibility list, `0xFFFF` as wildcard | `0x80` (inferred: warning) |
| `PARTDATA` | At least 6 bytes | `0x33`, `0x35` |
| | Partition exists | `0x14` |
| | Partition type matches, index below 5 | `0x48` |
| | At most one type-1 (code) partition | `0x49` |
| | Fits the physical partition | `0x15` |
| | Opens, writes, exact size | `0x17`, `0x50`, `0x51`, `0x67` |
| `APPUPFTR` | Exactly 128 bytes | `0x34` |
| Unknown tag | | `0x11` |
| Sync | ID not zero; matches unfinished transfer | `0x22`; `0x81` |

Code `0x81` matches the app's abort-and-resync behaviour, seen independently.

### How partition 1 gets applied (read)

1. `vm.app` writes it to a partition, then passes the index to firmware call 214 (`0x32B60`) and records status 4.
2. The stack firmware identifies the file (`0x5EC43`, accepting `CSR-dfu1` and `CSR-dfu2`) and checks each section's signature (`0x2055D`).
3. A system message reaches `0x32532`: 1 records success, 2 records failure. `0x32E11` reports status 2 or 4 as error `0x1E`.

So the app is built to come back and report a failed loader run. **A signature rejection should not brick the headset.** A file that passes the checks but contains broken code can.

Other integrity checks on the filesystem section (proven by `dfu_sign.py`):

| Check | Position | Handling |
|---|---|---|
| `vm.app` XOR to zero, word `0x13` | Uncovered | Updated directly |
| Image XOR to zero, trailer | Uncovered | Updated directly |
| CRC32 in words 68-69 | **Covered, so it must not change** | Other data adjusted to keep it |
| CSR-dfu2 file CRC | Not in any signature | Recalculated |

The stack section has a CRC but no signature field. Whether it can be changed has not been examined, and it should be left alone.

### Risk summary

| Change | Stands in the way | Risk |
|---|---|---|
| Announcements (partition 3.1) | Nothing | **Done on hardware** |
| Other partition 3 data | Nothing found in `vm.app`; voice mode may refuse it | Low to moderate. Bad audio data could be loud |
| Uncovered words in partition 1 | Nothing found | **Done on hardware** with a 4 s pairing hold. Each new edit can still break the code |
| Covered words in partition 1 | Signature | Rejected with `0x1E`, by the code as read |
| Accepted but faulty code | Nothing | **High without SPI** |

## Patching

`tools/dfu_sign.py patch` edits `vm.app` words, refusing any covered word and refusing its own output unless every check still passes. It keeps the CRC valid by solving for three uncovered words inside a debug source-path string. Those strings are passed to the assert helper `0x1BA73`, which only tests its first argument and never reads the string (read).

Test on the SPLC file: three timing edits changed 14 bytes, and all checks pass.

Not every word can be edited directly. Larger logic changes would have to be built from small edits in uncovered runs of up to 15 words, jumping between them. That is possible in principle, and nothing has been built that way yet.

## Buttons and touch

### Timing table (read)

At word `0x3771B`, descriptors at `0x3776C`, indexed by input number. Each entry is `(duration ms, kind, event)`. Kind 0 fires while held, kind 1 on release (inferred). Bit 15 of a duration means auto-repeat (read): the entry fires, then reschedules itself, at half the second word. `0x83B6` with 300 means after 950 ms, then every 150 ms. This table was previously read as double press, which was wrong: the firmware has no double-press detector. See [`double-press.md`](double-press.md).

`U` means uncovered, so directly editable. `S` means signed. Columns: duration / parameter / event.

| Input | Word | Entry | Event | Edit |
|---|---|---|---|---|
| 0 | `0x3771B` | 15 s, hold | `0x25` | S S S |
| 0 | `0x3771E` | 15 s, release | `0x26` | S S S |
| 1 | `0x37721` | 1.5 s, hold | `0x13` | S S S |
| 1 | `0x37724` | 1.5 s, release | `0x14` | S S S |
| 1 | `0x37727` | 7 s, hold | `0x15` | S S U |
| 1 | `0x3772A` | 7 s, release | `0x16` | **U U U** |
| 2 | `0x3772D` | 260 ms, hold | `0x0B` | **U U U** |
| 2 | `0x37730` | 260 ms, release | `0x0C` | **U U U** |
| 5 | `0x37733` | 800 ms, hold | `0x35` | **U U U** |
| 5 | `0x37736` | 800 ms, release | `0x36` | U U S |
| 5 | `0x37739` | Repeat: 950 ms, then every 150 ms | `0x35` | S S S |
| 5 | `0x3773C` | 1.5 s, hold | `0x37` | S S S |
| 5 | `0x3773F` | 1.5 s, release | `0x38` | S S S |
| 5 | `0x37742` | 2 s, hold | `0x02` | S S S |
| 5 | `0x37745` | 2 s, release | `0x39` | S S S |
| 5 | `0x37748` | 3 s, release | `0x3A` | S U U |
| 6 | `0x3774B` | 800 ms, hold | `0x3B` | **U U U** |
| 6 | `0x3774E` | 800 ms, release | `0x3C` | **U U U** |
| 6 | `0x37751` | Repeat: 950 ms, then every 150 ms | `0x3B` | **U U U** |
| 6 | `0x37754` | 1.5 s, hold | `0x3D` | **U U U** |
| 6 | `0x37757` | 1.5 s, release | `0x3E` | U S S |
| 6 | `0x3775A` | 2 s, hold | `0x05` | S S S |
| 6 | `0x3775D` | 2 s, release | `0x3F` | S S S |
| 6 | `0x37760` | 3 s, release | `0x40` | S S S |
| 7 | `0x37763` | 260 ms, hold | `0x08` | S S S |
| 8 | `0x37766` | 2 s, hold | `0x19` | S S S |
| 8 | `0x37769` | 2 s, release | `0x1A` | **U U U** |

The first 12 descriptor words are uncovered, which covers inputs 0 to 5. Pointing one of those at a replacement table built in uncovered words frees its signed entries. `tools/dfu_sign.py pairing` does this for input 1, and works on a real headset.

### Input path (read, meanings inferred)

- **Engine:** `0x114FD`-`0x11E00`, task at data `0x8E47` with handler `0x11524`. It posts one delayed message per table entry, and sends the event to the application task (data `0x7DCA`).
- **Input numbers:** table at data `0x801A` maps bit `n` to input `n`.
- **Digital inputs:** messages `0x8023` and `0x8025` (inferred: pin changes).
- **Analog ladder:** PS key 686 holds 9 rising thresholds up to `0x3FF`, then a code for each band. Bands map to input bits `0x04`, `0x08`, `0x10`, `0x20`, `0x40` and `0x400`, or "released". `0x118F4` and `0x119C1` match analog readings against this table.

### Touch panel (read for the gate, inferred for the rest)

MDR's `TOUCH_PANEL_SETTING` (`D8 D2`) reaches `0x18055`, which sets or clears bit 15 of config word `0x49` (`0xAC54`, `0xAC5F`). Its only input-related reader is `0xAC6B`, called from `0x119C1`, which then ignores analog-ladder events while touch is off.

So **touch gestures most likely reach the chip as levels on the analog ladder, from a separate touch controller**, and flow through the same timing engine. If so, gesture recognition itself happens outside this firmware. Events `0x0D`-`0x10` go to one handler (`0x8EE5`), which could be the four swipe directions, but those events are also used elsewhere, and this is not established.

### What events do (read)

The application's switch is at `0x920A`, table at data `0x7E49`. Most button events are no-ops there and are probably handled elsewhere: 0x13 and 0x15 have real handlers, 0x35 and 0x3B share `0x8DD1`, and 0x14 and 0x16 share a handler.

**Candidates for the NC/AMBIENT button (inferred, untested):** inputs 5 and 6 aren't it. They call the same function with opposite arguments, `0x8DD1`'s callers at `0x922C` and `0x9232`, and auto-repeat, which fits a held volume gesture. Input 2 has a click and a 260 ms hold, both reaching `0x963D` with different arguments. Input 8 has a 2 s hold, whose duration is signed. Inputs 2, 5 and 6 also share a function at their press events (arguments 4, 1 and 2), so they may be one family, which counts against input 2 being a separate physical button.

**Still needed:** which physical control each input is, and what each remaining action does. Input 1 is probably the power button (inferred, see below). The rest is best settled by a timing test on hardware.

### The pairing hold (read for the flow, inferred for the meaning)

The handlers split on the connection state (`0x12C6A`) and the power state object at `0x581`. From a powered-off start:

| Event | Handler | Behaviour |
|---|---|---|
| `0x13`, 1.5 s | `0x92F8` | Checks whether PS key `650 + 0x8D` holds a value (`0xB02D`). If it does, selects mode 1 through `0x9807`, and doesn't start yet. If not, selects mode 2 and starts after 1.5 s |
| `0x14`, released | `0x9455` | If a device was stored, starts mode 1 |
| `0x15`, N s, still held | `0x93FB` | If mode 1 is selected, switches to mode 2 and starts at once |
| `0x16`, released | `0x9455` | Doesn't start anything: `0x15` already did |

So **mode 2 is what the firmware picks when no device is stored, and what a long hold switches to**. That fits pairing mode.

**Confirmed on hardware:** with a 4 s value, the owner measured about 6 s from off to pairing, and describes it as much faster than before. The extra 2 s or so is probably boot time, since the timer can't run until the chip has booted. The owner also recalls power-on taking longer than the table's 1.5 s, which fits. The time while already on, which should have no boot delay, has not been measured.

The flow depends on the order of these events, not their exact times. The only timer involved is the table's. So a shorter hold should work the same way, as long as it stays above the 1.5 s entry.

**Other effects of a shorter hold:**

- A normal power-on must be released before the new time, or it becomes pairing.
- **Powering off gets a shorter window.** On a real headset, holding the button while on gives the power-off announcement at 1.5 s, and still holding at 7 s turns it back on. In the code, `0x13` sends internal message 4 and sets a flag, and `0x15` clears that flag and runs `0x9A49` (inferred: cancels the power-off). With a 3 s hold, the button must be released between 1.5 s and 3 s to power off.

**The 60 s timer (read, purpose inferred):** the same `0x15` path calls `0x98FE`, which sets word 4 of the object at `0x54E` and schedules event `0x28` for 60 s later. Event `0x28` only clears that word. While it is set, each event goes to `0x990F`, which compares it with a stored list starting at data `0x7DDA`. If the whole list is matched, it calls `0x96EB(0x21)`, sets a state word to 1, and runs `0x14C32`, which drives the LED candidate `0xF685`. In that state, the event dispatcher only passes `0x12` and `0x13` on. So this looks like a hidden sequence that opens a special mode. The owner recalls having used such a mode, but not why. The 14-entry list is `12 16 00 01 00 02 03 04 03 05 11 12 11 13`, and most of those aren't events from the button timing table, so it can't yet be translated into physical actions. `0x99CA` opens a similar 60 s window, with its own counter. No Bluetooth search was found on this path, but a separate 60 s search elsewhere has not been ruled out.

**Long timers (read, link to behaviour inferred):** `0x1875C` and `0x18795` handle the values 300, 1800 and 3600, which match the app's auto power off settings of 5, 30 and 60 minutes. The owner sees the headset turn off after about 30 minutes. What starts and resets that timer has not been traced. The firmware also has 5 and 10 minute timers in milliseconds, whose purpose is unknown.

**The patch (`tools/dfu_sign.py pairing`):** the 7 s duration words are signed in place, but input 1's descriptor is not. So it writes a copy of the table, with the new time, into uncovered words `0x394E9`-`0x394F4`, then repoints the descriptor. The reasoning behind it, now backed by a working headset:

- The engine (`0x11524`) reads each descriptor through a pointer and a count, and steps by 3 words. There is no direct reference to the table and no range check on the pointer (read).
- The copy sits inside `platform/csr867x/dsp_audio.c`. Five call sites refer to that string with the same address mapping the table uses, so the new pointer is valid data (read). All pass it to `0x1BA73`, which never reads it.
- The original table stays in place, unused. Bit 15 of a duration is masked off, and values up to 15 s don't reach it.

**Other timings (`tools/button_timing.py`, also in the web UI):** the short hold moves with the long hold, in the same relocated table. Input 2's 260 ms and the 800 ms entries of inputs 5 and 6 are unsigned in place. Input 5's table can't move: it needs 24 contiguous words, and unsigned runs stop at 15. Each build is pressed through the real engine in the emulator, which checks event order and times, not what the events do on a headset.

## Announcements

**Table (read):** data `0x998C`, 3 words per entry: file, alternative file, flag. In order: 0 power on, 1 power on with low battery, 2 power off, 3 link loss, 4 low battery, 5 recharge and power off, 6-7 max volume, 8 pairing, 9 ringtone, 10 reject call, 11 connected, 12 disconnected, and so on.

**Playing one (inferred):** `0x14EA1(index)` is called from about 40 places, usually with a constant. No direct link from it to the table has been traced. The evidence is that the numbers fit: `0x12F54` passes 11 and `0x12FC0` passes 12, which match connected and disconnected, and `0x12AF2` passes 9, the ringtone. Index 8, pairing, is passed from `0x9063`, `0x10BDD` and `0x11CE3`. Those sites have not been traced, and neither has where the power on and off sounds are played.

## LEDs

### Pattern table (read, meaning inferred)

Data `0x8C06` holds a base pointer for each of 3 channels: `0x8B43` (26 patterns, masks 1 and 2), `0x8BC5` (7 patterns, mask 4) and `0x8BE8` (6 patterns, mask 8). `0xF4C7` and `0xF6A8` multiply a pattern number by 5 and add this base, which explains why no code refers to the table directly. Each descriptor is 5 words: step list pointer, step count, then a repeat-like word, the channel's mask set, and flags. Steps are `(mask, duration ms)` pairs. `0xF6A8` applies each step through `0xF685`, which handles masks 1 and 2 with firmware call 171, and schedules the next step with the duration as a delayed message. What masks 4 and 8 drive is unknown.

**Choosing a pattern (read, meanings inferred):** `0xF75B` picks the idle pattern on channel 0. Branch conditions depend on how the disassembler shows `bcc`, so the connection state ranges are approximate.

| Condition | Pattern | Steps |
|---|---|---|
| Two flags in `0x57F` word 11 not both set | 1 | Off |
| Word 0 of the object at `0x58C` is 3 (inferred: updating) | 23 | 650 ms off, 650 ms on |
| Connection state 2 (inferred: pairing) | 7 | Double flash, 1.25 s period |
| Connection state 9 | 20 | One flash every 5 s |
| `0x588` word 4 is 5 (inferred: incoming call) | 11 | 100 ms on, 200 ms off |
| `0x588` words 3 and 4 are both at least 3 | 8 | One flash per second |
| Flag bit 15 set (`0xF653` sets it, with a 5 s message) | 10 | 150 ms on, 150 ms off |
| Otherwise, connected states | 9 | One flash every 5 s |

**Fixed patterns:** `0xF619(n)` starts one directly. `0x12AF2` plays sound 9 (ringtone) and starts pattern 11, which agrees with the table above.

**Link to pairing:** `0x10BDD` plays sound 8 (pairing), or 44 (`power_on_factory`) if a flag is set, sets connection state 2, and schedules a message for 3 s later. So the pairing sound and pattern 7 share a state. The link from mode 2 to `0x10BDD` has not been traced.

Pattern 23 (`0x8BB6`) points to `0x8B11`: `(0, 650)`, `(1, 650)`. During a firmware upload on a real headset, the blue LED was measured from video at 0.66 s on and 0.65 s off. That matches mask 1 being blue, and pattern 23 being the one shown. `0xF75B` selects it when word 0 at `0x58C` is 3, but what sets that word to 3 has not been traced. Mask 2 is probably red (inferred).

### Output path

**Strong candidate, not proven.** Firmware call 171 is called as `(channel, 0, on/off)` from three wrappers:

| Function | Behaviour | Callers |
|---|---|---|
| `0x10358(value, mask)` | Channels 0, 1, 2 by mask bits; bit 3 goes to `0xF7F6` | A debug command at `0xFE67` |
| `0xF7F6(mask, value)` | Channels from a table at data `0x8C78` | 14 |
| `0xF685(mask, value)` | Channels from data `0x514` | 13 |

The shape fits `LedConfigure(led, LED_ENABLE, value)`. But this could be a different on/off output, and no blink patterns have been found. Callers include power and battery code around `0xF600`, which fits status LEDs.

PS key 687 was previously guessed to be an LED table. Nothing supports that.

## Earlier reasoning kept for reference

Factoring the 1024-bit RSA key is not realistic for an individual, and it is unnecessary given the digest flaw. With SPI access, there are two more routes: write flash directly, or erase PS keys 350-354, the verification key. Neither has been tried, and the pad locations are unknown.

## Firmware calls

- **Mechanism (read):** 143 stubs in words `0x40`-`0x27F` load a number into `X` and jump to `0x286`.
- **Data addresses (read):** `address = file word - 0x2E943`.

| Call | Evidence | Likely meaning |
|---|---|---|
| 16, 17 | Delays such as 2000 ms, message IDs | Send message, delayed or not |
| 23, 24 | Key numbers and a buffer | Store, read settings. Key `n` is PS key `650 + n` |
| 46, 48 | Input task object | Register for pin messages |
| 50 | 32-bit value in two halves | Read pins |
| 51, 53, 55, 57 | Masks such as `0x2000`, `0x8000` | Set pin direction and output |
| 131, 134, 136 | Filenames | Open files |
| 149 | Device addresses `0x22`, `0x68` | Peripheral bus |
| 171 | See LEDs | Likely LED control |
| 206, 207, 220, 221 | Partition index and position | Partition info, open, configure, position |
| 214 | Code partition index | Start the loader |

Bus address `0x68` is an audio chip, since sample rates are written to it. Address `0x22` is unidentified.

## Regional differences

See [`protocol/04-verification.md`](protocol/04-verification.md). Patches must start from the file matching the headset's service ID.
