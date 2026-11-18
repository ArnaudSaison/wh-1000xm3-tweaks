# MDR transport

## RFCOMM services

| Service | UUID | Used for |
|---|---|---|
| MDR v1 | `96CC203E-5068-46AD-B32D-E316F5E069BA` | Control protocol. Confirmed on a real XM3. |
| MDR v2 | `956C7B26-D49A-4BA8-B03F-B17D393CB6E2` | Control protocol on newer models. The app tries this one first. |
| GAIA | `7B265B0E-2232-4D45-BEF4-BB8AE62F813D` | Firmware and voice-package transfer. |
| SPP | `00001101-0000-1000-8000-00805F9B34FB` | Alternative GAIA transport in the library. Not selected by the update code. |

The channel number is resolved through SDP. The MDR socket uses the standard secure RFCOMM call; the GAIA socket uses the insecure variant. Both need a paired device in practice.

The XM3 has a CSR8675, so its update path is the CSR one, not the BLE "Tandem" path used by newer models.

## Frame

```
3E | type | seq | length:u32be | payload | checksum | 3C
     \_____________ escaped between the markers ____/
```

- `checksum` is the low byte of the sum of `type` through the end of `payload`, computed **before** escaping.
- `length` covers the payload only.
- Escaping: `3C`, `3D`, `3E` are sent as `3D 2C`, `3D 2D`, `3D 2E`. The receiver ORs `0x10` onto the byte after `3D`.
- Frames larger than the transport limit are split across writes with no extra framing. A receiver treats the link as a byte stream and ignores anything before a `3E`.

Worked example, `CONNECT_GET_PROTOCOL_INFO`:

```
3E 0C 00 00 00 00 02 00 00 0E 3C
   |  |  |_________| |___| |
   |  |  length = 2  |     checksum = 0C + 02
   |  seq            payload: command 00, fixed value 00
   DATA_MDR
```

## Frame types

| Value | Name | Receiver acks | Notes |
|---|---|---|---|
| `01` | ACK | no | Empty payload |
| `0C` | DATA_MDR | yes | **Table 1** commands |
| `0E` | DATA_MDR_NO2 | yes | **Table 2** commands |
| `1C` | SHOT_MDR | no | Table 1, unacknowledged |
| `1E` | SHOT_MDR_NO2 | no | Table 2, unacknowledged |
| `00 02 07 09 0A 0D 27 2C 2D` | other DATA types | yes | Not handled by the v1 receiver |
| `10 12 19 1A 1D` | other SHOT types | no | Not handled by the v1 receiver |

Table 1 and table 2 are separate namespaces: `0x40` means `VPT_GET_CAPABILITY` in one and `VOICE_GUIDANCE_GET_CAPABILITY` in the other.

## Reliability

Stop-and-wait with a 1-bit sequence number, tracked independently in each direction.

**Sending.** Send with the current `seq` (initially 0), then wait for an ACK. The app waits 750 ms and resends the identical frame up to 10 times, then treats the link as dead. An ACK is valid only if its `seq` differs from the current one; that value becomes the new current `seq`. Only one frame is outstanding at a time.

**Receiving.** For a frame type that requires it, always answer with an ACK whose `seq` is `1 - received_seq`, including for duplicates. Then drop the frame if its `seq` equals the last one received (initially none). So a lost ACK causes a retransmission that is re-acknowledged but not processed twice.

ACK frames are therefore constant: `3E 01 00 00 00 00 00 01 3C` and `3E 01 01 00 00 00 00 02 3C`.

An unknown command must still be acknowledged. This explains the behaviour noted by others, where a headset appears to accept commands it silently ignores: **an ACK proves delivery, not acceptance.** Only a RET or NTFY proves a command took effect.
