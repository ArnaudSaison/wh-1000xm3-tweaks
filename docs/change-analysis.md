# What changes can we make, and would they work?

Offline verification of each modification we have tried or are considering, run by
`tools/change_report.py` (and locked by `tests/test_changes.py`). Each uses the
strongest offline check for its kind: the XAP emulator for code/logic, `dfu_sign`
for signature validity (it reproduces Sony's real signatures), and a structural +
checksum check for partition-3 data. Nothing here touches a device.

| Change | Kind | Keeps Sony signature? | Would it work? | Confidence |
|---|---|---|---|---|
| Pairing hold 7 s -> 4 s | vm.app data (table + pointer) | Yes (uncovered words) | Yes | **Proven** — emulator + hardware |
| Double-press -> pairing | vm.app code (hook and new code) | Yes (uncovered words) | In the emulator | Built; not run on hardware |
| Raise/remove volume cap | vm.app code constants | Partly (some words covered) | Partly OTA, rest needs SPI | Signature proven; runtime not emulated |
| Quieter announcements | partition-3 MP3 data | N/A (unsigned) | Yes | **Proven** on hardware |

## 1. Pairing hold (proven)
The emulator runs the real dispatch bytecode and reports event `0x15` (enter pairing)
firing at **7000 ms shipped -> 4000 ms patched**. Already flashed and measured on the
headset. This is the calibration anchor for trusting the emulator on the rest.

## 2. Double-press -> pairing

Superseded: see [`double-press.md`](double-press.md). The entries this section called double press are auto-repeat, and the firmware has no double-press detector, so a table edit can't do this. It takes a small code patch, now built and emulated but not run on hardware.

## 3. Regional volume cap (partly doable OTA)
The max-volume cap is **hard-coded in `vm.app`** (not only in the data tables) in the
function at `0x180ab`, which picks caps like `0xd5`/`0xd2`/`0xd7` per audio mode. Coverage
is mixed:

| Word | Meaning | Signature |
|---|---|---|
| `0x180b7`, `0x180f5` | `0xd5` caps | **uncovered — editable OTA** |
| `0x17fd4` | `0xd2` clamp-compare | **uncovered — editable OTA** |
| `0x180f8`, `0x1811a` | `0xd2` caps | covered — frozen |

`dfu_sign` proves raising the uncovered clamp-compare to `0xff` leaves every signature
valid, while editing a covered cap is refused. So a volume-limit change on the uncovered
paths is **signature-valid over the air today**; the covered paths need SPI (or an
injected override via the `xap_inject` trampoline). The *runtime* effect is best confirmed
by emulating `0x180ab` with its config helpers — a worthwhile follow-up, not yet done.

## 4. Quieter announcements (proven; this was the earlier "volume" change)
These are partition-3.1 MP3 data — **not XAP code and not under the signature** (the
signed region is the single partition-1 `CSR-dfu2` container; partition 3 sits outside
it). The only integrity check is a 16-bit XOR the build tool fixes. That is exactly why
the -9 dB upload was accepted on the headset. The emulator does not apply here — it runs
code, not audio — so the right offline check is the structural/checksum one.

## Takeaways
- The uncovered-words trick covers more than timing: **calls, some code constants (volume)
  and new code in unused functions are editable with Sony's signature intact.**
- Two distinct limits remain: *behavioural* certainty for stateful logic (needs a fuller
  engine model) and *reach* for covered words (needs SPI). Neither blocks the data-patch
  class of change.
