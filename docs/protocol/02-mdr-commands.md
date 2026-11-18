# MDR commands (v1)

The full name-to-value tables are in [`xm3/tables.py`](../../xm3/tables.py). This page covers the layouts that matter here.

The payload begins with the command byte and, for most commands, an "inquired type" that selects the sub-feature. Commands come in a regular family:

| Suffix | Direction | Meaning |
|---|---|---|
| `GET_CAPABILITY` / `RET_CAPABILITY` | host, then device | What the device supports |
| `GET_STATUS` / `RET_STATUS` / `NTFY_STATUS` | | Whether the feature is available now |
| `GET_PARAM` / `RET_PARAM` / `NTFY_PARAM` / `SET_PARAM` | | The current value, and changing it |

A `SET` is answered by an `NTFY`, not a `RET`. The device also sends `NTFY` by itself when the state changes, for example when a button is pressed.

Status bytes use `CommonStatus`, which reads backwards: `00` is ENABLE and `01` is DISABLE.

## Connecting

Send these in order. The headset is reported to ignore everything else until the first has been answered.

| Request | Reply |
|---|---|
| `00 00` protocol info | `01 00 <version:u16be>` |
| `02 00` capability info | `03 00 <version> <n> <unique id: n bytes>` |
| `04 <kind>` device info | `05 <kind> <n> <string>` |
| `06 00` supported functions | `07 00 <n> <n function codes>` |

Device info kinds: `01` model name, `02` firmware version, `03` series and color (`05 03 <series> <color>`), `04` instruction guide.

**The supported-functions reply is the authority on what your headset does.** Each byte is a `FUNCTION_TYPE`. For our goals the codes to look for are `F6` assignable settings, `39` voice guidance and `30` firmware update.

## Battery and audio

| Request | Reply / notification |
|---|---|
| `10 00` | `11 00 <level> <charging>`, also `13 00 <level> <charging>` |
| `18 00` codec | `19 00 <codec>`, also `1B` |
| `22 00 01` | Powers the headset off |

Charging: `00` no, `01` yes, `F0` unknown. Codec: `01` SBC, `02` AAC, `10` LDAC, `20` aptX, `21` aptX HD.

## Noise cancelling and ambient sound

`66 02` gets, `67` returns, `69` notifies, and `68` sets, all with this body for inquired type `02`:

```
<cmd> 02 <effect> <nc setting type> <nc value> <asm setting type> <asm id> <asm value>
```

| Field | Values |
|---|---|
| effect | `00` off, `01` on, `10` adjustment in progress, `11` adjustment complete |
| nc setting type | `00` on/off, `01` level, `02` dual/single/off |
| asm setting type | `00` on/off, `01` level |
| asm id | `00` normal, `01` voice focus |
| asm value | Level. Third-party captures show up to `0x14` (20). |

The published XM3 capture shows `68 02 11 01 02 01 00 00` for noise cancelling on and `68 02 11 01 00 01 00 14` for ambient level 20. The app treats `nc value` as a plain number, so the meaning of `00`/`01`/`02` (ambient, wind reduction, noise cancelling) comes only from that capture.

## Equalizer

| Request | Reply |
|---|---|
| `56 01` get | `57 01 <preset> <n> <n band values>` |
| `58 01 <preset> <n> <bands>` set | `59` notification |
| `50 01 <language>` capability | `51 ...` with preset list and band range |

Preset IDs include `00` off, `10` bright, `11` excited, `12` mellow, `13` relaxed, `14` vocal, `15` treble, `16` bass, `17` speech, `A0` custom, `A1`-`A5` user settings.

## Controls on the XM3: general settings

The XM3 does **not** list `ASSIGNABLE_SETTINGS`. Its control settings arrive through `GENERAL_SETTING`, where the headset describes each setting itself. Read from a real headset:

| Inquired type | Title | Kind | Values |
|---|---|---|---|
| `D1` | `ASSIGNABLE_KEY_SETTING` | list | `00` NC/ambient, `01` Google Assistant, `02` Alexa |
| `D2` | `TOUCH_PANEL_SETTING` | boolean | `01` on |

```
D0 <type> <language>     get capability
D1 <type> <fmt> <n> title <n> summary <kind> [<count> { <fmt> <n> title <n> summary }]
D6 <type>                get value
D7 <type> <kind> <value> current value (kind: 01 boolean, 02 list)
D8 <type> <kind> <value> set (from the app; never sent to real hardware by these tools)
D9 ...                   notification
```

These strings also appear in `vm.app`, with reduced lists stored next to the full one. That suggests the available options vary by region or firmware state. So through the protocol, the controls come down to these two settings. Everything else about controls needs firmware changes.

## Assignable keys (other models)

Not listed by the XM3, and documented here only for completeness. Inquired type `06` of the `SYSTEM` family.

```
F6 06                    get current
F7 06 <n> <n presets>    current, one preset per key
F8 06 <n> <n presets>    set
F9 06 <n> <n presets>    notification

F0 06                    get capability
F1 06 <key count> { key, key type, default preset, <preset count>
                    { preset, <action count> { action, function } } }
```

| Field | Values |
|---|---|
| key | `00` left side, `01` right side, `02` custom, `03` C key |
| key type | `00` touch sensor, `01` button |
| preset | `00` ambient control, `10` volume, `20` playback, `30` voice recognition, `31` Google Assistant, `32` Alexa, `33` Tencent, `FF` none |
| action | `00` single tap, `01` double, `02` triple, `10` tap and hold, `11` double tap and hold, `21`/`22` long press |
| function | `00` none, `01` NC/ambient off, `02` NC optimizer, `10` quick attention, `11`/`12` volume, `20` play/pause, `21`/`22` next/previous, `30`-`36` assistants |

A client picks a *preset* for a key and cannot compose its own action-to-function map. Press durations and arbitrary gestures have no command in either table.

## Announcements: voice guidance

Table 2 (frame type `0E`). Inquired type `01`.

| Request | Reply | Meaning |
|---|---|---|
| `40 01` | `41 01 <switch> <language switch> [<n> <languages>]` | Capability; `00` unsupported, `01` supported |
| `46 01 01` | `47 01 01 <on/off>` | Read on/off |
| `48 01 01 <on/off>` | `49` | Set on/off |
| `46 01 02` | `47 01 02 <language>` | Read language |
| `46 01 03` | `47 01 03 <n>` | "Required time" for an update; the XM3 reports 7 |
| `46 01 04` | `47 01 04 01 <n> category <n> ? <count> { <language> <n> service ID }` | Where to download each language |
| `46 01 05` | `47 01 05 <method> <BLE TX power> <battery threshold>` | Update parameters |
| `44 01 02 <status>` | `45 01 02 <status>` | Enter (`00`) or leave (`01`) language-update mode |

Languages: `01` English, `02` French, `03` German, `04` Spanish, `05` Italian, `06` Portuguese, `07` Dutch, `08` Swedish, `09` Finnish, `0A` Russian, `0B` Japanese, `0C` Simplified Chinese, `0D` Brazilian Portuguese, `0E` Traditional Chinese, `0F` Korean, `10` Turkish, `F0` Chinese.

Over this channel a client can only toggle announcements and select a language. Their content is changed by uploading a package, using the same mechanism as firmware. See [the voice guidance section](03-firmware-update.md#voice-guidance-packages).

No command for announcement volume or for playing an arbitrary sound exists in either table.

## Other families, not yet detailed

The tables include these, unused by our tools so far: `VPT` (`40`-`49`, sound position), `SENSE` (`70`-`74`), `OPT` (`80`-`89`, NC optimizer), `ALERT` (`90`-`99`), `PLAY` (`A0`-`A9`, playback control and metadata), `SPORTS` (`B0`-`BD`), `LOG` (`C4`, `C9`), `GENERAL_SETTING` (`D0`-`D9`), `AUDIO` (`E0`-`E9`), `TEST_COMMAND` (`FF`).

`TEST_COMMAND` deserves a look, since it may expose factory functions. It has not been analysed and should not be sent blind.
