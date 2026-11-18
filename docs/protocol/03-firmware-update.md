# Firmware update

Three layers: an MDR command opens update mode, GAIA carries packets over a second RFCOMM connection, and CSR's VM Upgrade protocol runs inside GAIA.

The headset receives the **entire `.bin` file unmodified**, including the `APPUHDR2` header. Nothing in the app inspects or checks a signature: it verifies the download's digest against the manifest, and all other validation happens in the headset. So a tool talking directly to the headset, like `uv run python -m xm3 flash`, removes the app and the update server from the path, but **does not bypass any check the headset performs**.

## Layer 1: update mode

```
-> 34 01 00   UPDT_SET_STATUS, FW_UPDATE_MODE, ENABLE
<- 35 01 00   UPDT_NTFY_STATUS, confirmed
...
-> 34 01 01   leave update mode (failure here is ignored by the app)
```

Before starting, the app reads the battery threshold with `36 08`, replying `37 08 <percent>`. It requires the battery level to be strictly above it. A real XM3 reports 50%, so it needs at least 51%. `xm3 flash` applies the same check, and uses 30% if the headset does not answer (our own choice, not Sony's).

Other `UPDT_GET_PARAM` (`36`) kinds: `02` category ID, `03` service ID, `04` nation, `05` language, `06` serial number, `07` BLE TX power, `09` update method, `0A` battery threshold while interrupting, `0B` device-binding ID.

The app keeps the MDR connection open during the transfer and treats its loss as a failure.

## Layer 2: GAIA

```
FF 01 flags length vendor:u16be command:u16be payload [xor]
```

- Maximum payload is 254 bytes.
- Bit 0 of `flags` appends an XOR of all preceding bytes. The app sends with flags `00`.
- A reply has bit 15 of `command` set. Its first payload byte is a status: `00` success, `01` not supported, `02` not authenticated, `03` insufficient resources, `04` authenticating, `05` invalid parameter, `06` incorrect state, `07` in progress.

Vendor `000A` (Qualcomm/CSR) is used throughout. Vendor `012D` (Sony's Bluetooth company ID) with command `0200` carries Sony's own requests for true-wireless models, not relevant to the XM3.

| Command | Name | Payload |
|---|---|---|
| `0640` | VM upgrade connect | none; the app ignores the reply status |
| `0641` | VM upgrade disconnect | none |
| `0642` | VM upgrade control | an upgrade packet (below) |
| `4001` | Register notification | event ID; `12` for upgrade packets |
| `4002` | Cancel notification | event ID |
| `4003` | Event notification | sent by the headset: `12` + upgrade packet |

## Layer 3: VM Upgrade

```
opcode:u8 length:u16be data
```

Host to headset travels in `0642`. Headset to host arrives in `4003` with event `12`.

| Op | Name | Direction | Data |
|---|---|---|---|
| `01` | START_REQ | to headset | |
| `02` | START_CFM | to host | not examined by the app |
| `03` | DATA_BYTES_REQ | to host | `size:u32be`, `skip:u32be` |
| `04` | DATA | to headset | `last:u8` then file bytes |
| `07` | ABORT_REQ | to headset | |
| `08` | ABORT_CFM | to host | |
| `0B` | TRANSFER_COMPLETE_IND | to host | |
| `0C` | TRANSFER_COMPLETE_RES | to headset | `00` |
| `0E` | IN_PROGRESS_RES | to headset | `00` |
| `0F` | COMMIT_REQ | to host | |
| `10` | COMMIT_CFM | to headset | `00` |
| `11` | ERROR_WARN_IND | to host | `code:u16be` |
| `12` | COMPLETE_IND | to host | |
| `13` | SYNC_REQ | to headset | last 4 bytes of the file's MD5 |
| `14` | SYNC_CFM | to host | first byte is the resume point |
| `15` | START_DATA_REQ | to headset | |
| `16` | IS_VALIDATION_DONE_REQ | to headset | |

The names are those in the app's log strings where present, and CSR's conventional names otherwise (inferred). The state machine itself is from the app.

### Sequence

```
open GAIA RFCOMM
-> 0640 connect
-> 4001 12                 register for upgrade events
-> SYNC_REQ(md5 tail)
<- SYNC_CFM(resume point)
-> START_REQ
<- START_CFM

resume point 0, DATA_TRANSFER:
  -> START_DATA_REQ
  repeat:
    <- DATA_BYTES_REQ(size, skip)
    -> DATA(last?, bytes)      advance by skip first; at most 250 bytes
  -> IS_VALIDATION_DONE_REQ
  <- TRANSFER_COMPLETE_IND     the headset has accepted the data
  -> TRANSFER_COMPLETE_RES(0)
  ** headset reboots **
  wait for it to return, reopen GAIA, wait 300 ms, repeat connect/sync/start

resume point 3, IN_PROGRESS:
  -> IN_PROGRESS_RES(0)
  <- COMMIT_REQ or COMPLETE_IND
  if COMMIT_REQ:
    -> COMMIT_CFM(0)
    <- COMPLETE_IND
```

Details that are easy to get wrong:

- Every packet sent in `0642` needs its GAIA ACK with status 0 before continuing, **except `TRANSFER_COMPLETE_RES`**. The app sends that one and does not wait, since the headset reboots. `DATA` is paced by both the ACK and the next `DATA_BYTES_REQ`.
- The app keeps acknowledging frames on the MDR link throughout the transfer, because the headset can send notifications at any time.
- At the end, the app simply closes the GAIA link. `0641` is not actually sent, because its code path checks that the link is still open and it has already been closed.
- `skip` is relative to the current position. It lets the headset resume a partial transfer.
- A requested `size` outside 0 to 250 is treated as 250.
- A `SYNC_CFM` with one byte or less of data is read as resume point 0.
- Error `0x0081` or `0x0023` during sync is answered with `ABORT_REQ`, wait for `ABORT_CFM`, then sync again, once. Any other code fails. The codes' meanings are not in the app.
- The other resume points (1, 2, 4) make the app fail on a single-device product such as the XM3.
- The app waits for the phone's A2DP reconnection before reopening GAIA. `xm3` retries the RFCOMM connection instead, up to 240 s.
- Timeouts in the app: 60 s per step, 240 s for validation and install.
- **Deliberate difference:** if `size` exceeds the bytes left, the app pads the chunk with zeros. `xm3` sends only the real remaining bytes.

### Point of no return

Everything before `TRANSFER_COMPLETE_RES` can be abandoned without affecting the running firmware, as far as this flow shows (inferred, not tested). Partition 1's signatures are checked later, by the loader: see [How partition 1 gets applied](../firmware-analysis.md#how-partition-1-gets-applied-read). So a rejected firmware file is expected to show up as error `0x1E` after the reboot. That comes from reading the code: no file has been rejected on hardware yet.

## Voice guidance packages

`CsrUpdateController` has two targets, `FW` and `VOICE_GUIDANCE`, sharing this exact transfer code. The only differences: update mode is entered with `44 01 02 00` (table 2) rather than `34 01 00`, and the file comes from a language-specific service ID.

The XM3 lists function `39` and reports a service ID per language: `VGIDCSR00` plus the language code in hex, under category `HP001`. English, for example, is `VGIDCSR0001`. They download from Sony's server with the same manifest format, using `tools/fetch_firmware.sh`.

**A package is a normal update file containing only partition 3.1, the announcements.** Sony's English package has header variant `ZARKOV77` and a zeroed footer, and its partition is byte-identical to partition 3.1 of firmware 4.5.2. It holds 41 audio files plus `voice/language.bin`.

This is the lowest-risk way to change announcements, because partition 1, the code, is not in the file at all. Sony's original package can be kept for restoring. `tools/build_quiet_prompts.sh` accepts one as input:

```sh
tools/build_quiet_prompts.sh 01_English_OTA_20181108_file.bin quiet_en.bin 6
uv run python -m xm3 --address <addr> flash --voice quiet_en.bin      # dry run
```

**Confirmed on hardware:** a real headset accepted and installed a modified package, with announcements lowered by 9 dB. Partition 3 has only an XOR checksum, which the tool corrects. After that upload, it falsely reported 0% battery and said to recharge, until it was power-cycled or charged. Which of those fixed it, and why it happened, is unknown.

## A firmware upload on real hardware

A patched 4.5.2 firmware (`tools/dfu_sign.py pairing`, 4 s hold) was uploaded from the web UI to a real SPLC headset that was already on 4.5.2:

| Time | Event |
|---|---|
| 18:27:24 | Firmware update mode entered, synchronised at resume point `DATA_TRANSFER` |
| 18:40:54 | All 3,776,802 bytes sent (13.5 minutes), and `TRANSFER_COMPLETE_IND` received at once |
| 18:40:55 | Reboot requested. The LED was off for a while, then steady blue |
| 18:43:17 | GAIA reconnected at resume point `IN_PROGRESS`, then the update completed |

The blue LED blinked 0.66 s on, 0.65 s off during the transfer. No error was reported, and the headset booted with the patch.

## The update server

Host `info.update.sony.net`, path `/<category>/<service>/info/info.xml`.

In app 13.3.0:

- The manifest URL is hard-coded to `https://`.
- Every `http://` URI inside a manifest is rewritten to `https://`.
- Downloads use `HttpsURLConnection` and cast to it, so a plain HTTP URL could not work anyway.
- The manifest has a header, a blank line, then a body. The body is AES- or 3DES-encrypted according to the header and verified with `digest(digest_hex(body) + service + category)`.
- There is no certificate pinning in this code, but also no `networkSecurityConfig`. With target SDK 36, the app trusts only system CAs, so a user-installed CA has no effect.

**Consequence:** a plain HTTP proxy cannot serve this app version. To intercept it, you would need to install a CA in the phone's *system* store, which requires root, and use TLS. `xm3` and `flasher/` avoid the problem by talking to the headset directly.

## Not determined

- What the headset does with a firmware file it rejects, or one that doesn't boot. See the accepted upload below.
- The meaning of most error codes. The parser's codes are listed in [04-verification.md](04-verification.md).
- The contents of `START_CFM` and the remaining bytes of `SYNC_CFM`.
- Whether the app's `IS_VALIDATION_DONE_REQ` is sometimes answered with a "not done yet" reply, as in CSR's general protocol. The app sends it once and waits only for `TRANSFER_COMPLETE_IND`.
