# Do the app, the firmware and the headset agree?

Checked against app 13.3.0, firmware 4.5.2 (`HDX-2916_V0452_SPLC_with_stack.bin`) and a real WH-1000XM3 running 4.5.2. Read-only commands, one voice package upload and one patched firmware upload were sent to the headset.

| Topic | App code | `vm.app` static | Real headset |
|---|---|---|---|
| RFCOMM service `96CC203E…` | yes | not checked | **works** |
| Frame markers, checksum, ACK, sequence | yes | markers seen | **works** on every exchange |
| Escape set `3C/3D/3E` to `2C/2D/2E` | yes | **matching routine** near word `0x13800` | no reserved byte known to have occurred, so not exercised for certain |
| Command values, 54 queries | yes | see below | **all but two answered** with the expected reply code |
| Custom button and touch settings | generic | **strings present** | **matches** |
| Voice package service IDs | generic | **`VGIDCSR00`, `HP001`, `MDRID2916` present** | **matches** |
| GAIA `0640/0642`, ACK bit 15 | yes | **constants present**, including `8640/8641/8642` | **works** in a voice package upload |
| Upgrade state machine | yes | see [firmware analysis](../firmware-analysis.md) | **works** in voice and firmware modes |

The two unanswered queries were `36 0A` and `A6 01`. They were acknowledged, which only proves delivery.

## How strong the static evidence is

`vm.app` stores strings at one character per 16-bit word, which is why plain text searches miss them. Scanning it for `cmp` constants supports the table but does not prove it:

- 41 of the app's host commands appear as compare constants in the dispatch code around words `0x165FE` to `0x185A8`.
- Others did **not** appear there, such as `36`, `42`, `52` and `56`, yet the headset answers all four. So this scan is incomplete, probably because of table-driven or arithmetic dispatch. **Absence from the scan says nothing about support.**
- The `GAIA 0640` family appears in a routine that also *compares against* their ACKs. That suggests `vm.app` can act as a GAIA client for those commands. Its purpose is unknown, and it is not evidence that this code handles the phone's upgrade traffic.
- The upgrade handler and the signature check were found later: see [`firmware-analysis.md`](../firmware-analysis.md).

## Differences found

- **No conflict between the three sources** was found in anything tested. The errors were in the earlier third-party notes.
- The headset's `ASSIGNABLE_SETTINGS` absence means the app's generic assignable-keys code does not apply to this model.

## Regional firmware

A headset's service ID decides which firmware it is offered. `uv run python -m xm3 --address <addr> survey` shows it. The 4.5.2 files for `MDRID291600` (global) and `MDRID291601` (SPLC) were compared file by file:

| Part | Difference |
|---|---|
| Voice and beeps, partition 3.1 | **None.** Byte-identical, so voice packages are not region-specific. |
| `volume_table/mode_1`, `mode_3`, `mode_5` | **SPLC has a lower volume table.** Entries are 3 units lower, for example a top value of `0xD2` against `0xD5`. |
| PS key 698 (app settings) | SPLC `0001 FFFF`, global `0000 0000`. Looks like a region flag (inferred). |
| PS key 10169 | One byte differs in each of two values. Meaning unknown. |
| `vm.app` | 4 bytes: the header checksum area, one immediate near word `0xC489`, and two bytes of a data value matching key 10169. |
| Signatures, CRCs, checksums | Differ as a consequence. |

**Do not flash the global file onto an SPLC unit**, or the reverse. It would change the volume limit, which may exist for regulatory reasons. "SPLC" presumably refers to sound pressure limiting, but nothing in the files states that. `xm3 flash` compares the file's PS key 698 with the headset's service ID, and refuses a mismatch or an unknown service ID.

These files came from Sony's server. No flash contents have been read from a headset: the protocols offer no such command. So the installed firmware is only *reported* as 4.5.2.

## The update code in `vm.app`

Data addresses were calibrated with a pointer table of `volume_table/` strings: `address = file word - 0x2E943`. Word offsets below refer to firmware 4.5.2.

Found, from reading the disassembly:

- Three getters at word `0x32E7E` return the strings `APPUHDR2`, `PARTDATA` and `APPUPFTR`. So `vm.app` itself parses update files.
- Starting at `0x33293`, the parser compares the 8-byte tag and applies a length rule:

| Tag | Rule | Error returned |
|---|---|---|
| `APPUHDR2` | length at least 14 | `0x32` |
| `PARTDATA` | length at least 6 | `0x33` |
| `APPUPFTR` | length exactly 128 | `0x34` |
| anything else | | `0x11` |

- From `0x33314`, the header handler does an 8-byte compare that returns `0x13` on mismatch, then walks a list of 4-byte entries, treating `0xFFFF` as a wildcard. That matches the variant and the `(major, minor)` compatibility list in the file header (inferred from the shape of the code and the file).
- The partition handler's further checks are decoded in [`firmware-analysis.md`](../firmware-analysis.md).
- The footer handler checks the length and allocates a buffer, returning `0x3E` if that fails.
- `0x3376B` refers to `0x6640`, `0x6641` and `0x6642`, which look like the GAIA upgrade commands mapped to internal message IDs (inferred).

Sony's own packages carry an all-zero footer, so that footer cannot be holding a working signature.

Not yet found: how the partition 3 XOR checksum is checked, and what happens to the footer buffer.

## Reproducing

```sh
uv run python -m xm3 --address <addr> survey     # read-only
uv run python -m xm3 --address <addr> controls   # read-only
```
