# AGENTS.md

Technical map of the project for developers and AI coding assistants. The human-facing overview is [README.md](README.md).

Target: Sony WH-1000XM3, Qualcomm/CSR8675, model `MDRID2916`, OTA variant `ZARKOV77`, firmware 4.5.2.

## Rules

- **Python runs through uv only:** `uv run …`, `uv sync`, `uv add`. Never `pip` or a bare `python`.
- **Never commit Sony material:** firmware, app (APK/XAPK), decompiled output, extracted sounds, or keys. They live in the git-ignored `firmware/`, `data/` and `work/` folders. Check `.gitignore` before adding a new kind of file.
- **The info.xml AES keys are not in the code.** `tools/sony_infoxml.py` reads them from `XM3_INFOXML_KEYS` (hex, comma-separated, first one encrypts). Don't hardcode them in code, tests or docs.
- **Writing to the headset is opt-in.** CLI `flash` is a dry run without `--really-flash`; the web UI needs a typed confirmation word. Keep it that way, and keep the offline checks (model, version, battery, signatures, checksums, region) in front of every upload.
- **Label claims by evidence**, as the docs do: *read* (seen in the disassembly), *inferred*, *emulated* (run in `tools/xap_machine.py`), or *on hardware*. Don't present an emulated result as tested.
- Tests: `uv run python -m unittest discover -s tests` (about 6 minutes; the emulator tests are the slow part). Tests that need Sony files skip when `firmware/` is absent.

## What is verified on a real headset

| Change | Status |
|---|---|
| Read-only queries (`info`, `survey`, `controls`) | on hardware |
| Voice package upload (Sony original, and −9 dB quiet build) | on hardware. Afterwards the headset once reported 0% battery until power-cycled or charged (cause unproven) |
| Firmware upload with long hold 4 s | on hardware, signatures stayed valid |
| Firmware with short hold 1 s and long hold 3 s | on hardware |
| Double press for pairing, first version | on hardware |
| Double press, current version (waits 3 s before the pairing sound) | emulated only |
| CLI `flash` for firmware | simulated headset only (it shares the upload code with the UI) |

No SPI recovery path is set up. A firmware file that is accepted but doesn't boot cannot be recovered yet.

## Layout

| Path | Contents |
|---|---|
| `xm3/` | Independent implementation of the Bluetooth protocols (MDR over RFCOMM, GAIA upgrade), package parsing, and the CLI (`uv run python -m xm3 --address AA:BB:CC:DD:EE:FF info`) |
| `flasher/` | Flask web UI on 127.0.0.1:8750 ([README](flasher/README.md)); working files in `work/ui/` |
| `tools/` | Offline analysis, patching and emulation tools (table below) |
| `tests/` | Unit tests, plus `sim_headset.py`, a simulated headset for MDR and GAIA |
| `docs/` | Protocol and firmware write-ups; `docs/images/` holds README screenshots |

CLI examples:

```sh
uv run python -m xm3 --address AA:BB:CC:DD:EE:FF info            # read-only summary
uv run python -m xm3 --address AA:BB:CC:DD:EE:FF survey          # read-only: raw replies for every feature
uv run python -m xm3 --address AA:BB:CC:DD:EE:FF controls        # read-only: custom button, touch panel
uv run python -m xm3 --address AA:BB:CC:DD:EE:FF flash --voice quiet_en.bin  # dry run: checks only
```

## Getting the firmware

```sh
tools/fetch_firmware.sh firmware/      # needs XM3_INFOXML_KEYS; downloads the latest (4.5.2) and verifies the SHA-1
```

Sony's server is `http://info.update.sony.net/HP001/<service>/info/info.xml` (service `MDRID291600` global, `MDRID291601` SPLC, `MDRID291602` CN; voice packages are `VGIDCSR00<language>`). The file is AES-128-ECB (`ENC0003`); its digest is `sha1(sha1hex(xml) + service + "HP001")`. With the keys, `tools/sony_infoxml.py` decrypts and re-encrypts Sony's real file byte-identically.

| Version | File | SHA-1 | Source |
|---|---|---|---|
| 4.5.2 (latest) | `HDX-2916_V0452_with_stack.bin` | `442ac8d9…a195` | Sony server |
| 4.1.1 | `HDX-2916_V0411.bin` | `683a3830…aece` | MDR_Proxy archive |
| 2.0.0 | `HDX-2916_V0200.bin` | `952e4c58…51c8` | MDR_Proxy archive |

## Anatomy of the update file

```
APPUHDR2   Qualcomm ADK OTA update header (variant "ZARKOV77", v2.1)
PARTDATA   partition 1   : CSR-dfu2 file "HDX-2916_OTA_WITH_STACK"           ── signed, see below
             ├─ CSRbcfw1 : Bluetooth stack (XAP code, CRC ok)
             ├─ PS stack : 175 PS keys  (+ RSA-1024 public key, keys 350-354)  + 128-byte field
             ├─ PS app   :  59 PS keys                                          + 128-byte field
             └─ fsr_dfu1 : filesystem                                           + 128-byte field
                   ├─ vm.app                    (application, XAP bytecode, 482 KB)
                   └─ flash_info/sony_FlashInfo.kap (DSP)
PARTDATA   partition 3.1 : "File" FS  voice prompts + beeps (MP3)            ── XOR-16 checksum only
PARTDATA   partition 3.2 : "File" FS  volume / ANC / EQ / ecia tables        ── XOR-16 checksum only
PARTDATA   partition 3.3 : "File" FS  Kalimba DSP plugins (.kap)
APPUPFTR   128-byte OTA footer: all zero (the OTA wrapper itself is unsigned)
```

**Signing:** partition 1 is RSA-1024 (e = 3, no padding), but the digest covers only words 0-16 of each 32-word block, so about 47% of it can change with Sony's signature still valid. Partition 3 is unsigned. `tools/dfu_sign.py` verifies this offline. Details: [`docs/firmware-analysis.md`](docs/firmware-analysis.md).

## Quieter announcements

41 MPEG-2 Layer III files (16 kHz, 32 kbit/s, normalized to ~0 dBFS) under `beep/` and `voice/` in partition 3.1. Sony ships muted stand-ins, `silent_*.mp3`.

```sh
tools/build_quiet_prompts.sh firmware/HDX-2916_V0452_with_stack.bin quiet.bin 6   # 6 steps × 1.5 dB = -9 dB
```

`mp3gain_lite.py` lowers each granule's `global_gain`: no re-encoding, same size, so the filesystem layout doesn't move. A rebuild with no change reproduces the original byte-for-byte. The web UI installs this through Sony's separate voice package, which holds only partition 3.1.

Unused lead: `volume_table/beep.bin` (partition 3.2) repeats `0000 00df 000b`; `0x00df` is above the music table's max (`0x00db`), likely the fixed announcement gain. Untouched, meaning unproven.

## Buttons, touch, LEDs

See [`docs/firmware-analysis.md`](docs/firmware-analysis.md) and [`docs/double-press.md`](docs/double-press.md).

- **Press timing:** the table is mapped; about half its entries can be edited with the signature intact (`tools/dfu_sign.py patch`, `tools/dfu_sign.py pairing`).
- **Double press:** the firmware has no double-press detector (bit-15 entries are auto-repeat). `tools/double_press.py` adds code in unsigned words, rehearsed in `tools/xap_machine.py`.
- **Touch:** most likely analog levels from a separate controller, through the same timing engine. **LEDs:** a candidate control path, not proven.

## Recovery

CSR8675 SPI debug port ([csr-spi-ftdi](https://github.com/lorf/csr-spi-ftdi) + BlueSuite) is the only known route. Pad locations on the XM3 are unknown.

## Tools (`tools/`)

| Tool | Role |
|---|---|
| `fetch_firmware.sh` | download firmware from Sony and verify SHA-1 (needs `XM3_INFOXML_KEYS`) |
| `sony_infoxml.py` | decrypt / encrypt `info.xml` (needs `XM3_INFOXML_KEYS`) |
| `appuhdr.py` | split the OTA file (`APPUHDR2`/`PARTDATA`/`APPUPFTR`) |
| `csrdfu.py` | split a `CSR-dfu2` file (stack, PS keys as JSON, filesystem) and check CRCs |
| `csrfs.py` | extract a read-only CSR filesystem (`File`) |
| `mp3gain_lite.py` | lossless MP3 attenuation |
| `repack.py` | rebuild the OTA file with in-place file replacements and a corrected XOR checksum |
| `build_quiet_prompts.sh` | full chain for quieter announcements |
| `xapdis.py` | minimal XAP disassembler (port of radare2) |
| `xapanalyze.py` | firmware calls, functions, callers and annotated listings for `vm.app` |
| `dfu_sign.py` | verify Sony's signatures offline, and patch `vm.app` words without breaking them |
| `button_table.py`, `button_timing.py` | locate, print and edit the press-duration table |
| `xap_machine.py` | run real `vm.app` event flows on the PC, with a stack and message queue |
| `double_press.py` | build the double-press patch |
| `xap_emulator.py`, `xap_asm.py`, `xap_inject.py` | run single functions, assemble small snippets, link them across unsigned words ([notes](docs/emulator.md)) |
| `firmware_viz.py` | render an interactive map of `vm.app` to `work/viz/` (output contains firmware-derived data: never commit it) |
| `change_report.py`, `inject_demo.py`, `probe_demo.py` | offline checks and demonstrations ([notes](docs/change-analysis.md)) |
| `apk_extract_tables.py` | regenerate `xm3/tables.py` from a jadx decompilation of the app |
| `btsnoop_mdr.py` | decode MDR and GAIA traffic from an Android HCI snoop log |
