"""Builders and parsers for the MDR v1 command payloads used by the WH-1000XM3.

Every payload starts with the command byte, usually followed by an "inquired
type" selecting the sub-feature. See docs/protocol/02-mdr-commands.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import tables as t

_C1 = t.T1_COMMAND
_C2 = t.T2_COMMAND


def name_of(table: dict[str, int], value: int) -> str:
    for name, code in table.items():
        if code == value:
            return name
    return f"0x{value:02X}"


def command_name(payload: bytes, table: int = 1) -> str:
    return name_of(_C1 if table == 1 else _C2, payload[0]) if payload else "(empty)"


# --- Requests -----------------------------------------------------------------

def get_protocol_info() -> bytes:
    return bytes([_C1["CONNECT_GET_PROTOCOL_INFO"], 0x00])


def get_capability_info() -> bytes:
    return bytes([_C1["CONNECT_GET_CAPABILITY_INFO"], 0x00])


def get_device_info(kind: str) -> bytes:
    return bytes([_C1["CONNECT_GET_DEVICE_INFO"], t.DEVICE_INFO_TYPE[kind]])


def get_support_function() -> bytes:
    return bytes([_C1["CONNECT_GET_SUPPORT_FUNCTION"], 0x00])


def get_battery() -> bytes:
    return bytes([_C1["COMMON_GET_BATTERY_LEVEL"], t.BATTERY_INQUIRED_TYPE["BATTERY"]])


def get_codec() -> bytes:
    return bytes([_C1["COMMON_GET_AUDIO_CODEC"], 0x00])


def power_off() -> bytes:
    return bytes([_C1["COMMON_SET_POWER_OFF"], 0x00, 0x01])


def get_ncasm() -> bytes:
    kind = t.NCASM_INQUIRED_TYPE["NOISE_CANCELLING_AND_AMBIENT_SOUND_MODE"]
    return bytes([_C1["NCASM_GET_PARAM"], kind])


def set_ncasm(effect: int, nc_setting_type: int, nc_value: int,
              asm_setting_type: int, asm_id: int, asm_value: int) -> bytes:
    kind = t.NCASM_INQUIRED_TYPE["NOISE_CANCELLING_AND_AMBIENT_SOUND_MODE"]
    return bytes([_C1["NCASM_SET_PARAM"], kind, effect, nc_setting_type, nc_value,
                  asm_setting_type, asm_id, asm_value])


def get_eq() -> bytes:
    return bytes([_C1["EQEBB_GET_PARAM"], t.EQEBB_INQUIRED_TYPE["PRESET_EQ"]])


def set_eq(preset: int, bands: bytes = b"") -> bytes:
    return bytes([_C1["EQEBB_SET_PARAM"], t.EQEBB_INQUIRED_TYPE["PRESET_EQ"], preset, len(bands)]) + bands


def get_assignable_capability() -> bytes:
    return bytes([_C1["SYSTEM_GET_CAPABILITY"], t.SYSTEM_INQUIRED_TYPE["ASSIGNABLE_SETTINGS"]])


def get_assignable() -> bytes:
    return bytes([_C1["SYSTEM_GET_PARAM"], t.SYSTEM_INQUIRED_TYPE["ASSIGNABLE_SETTINGS"]])


def set_assignable(presets: list[int]) -> bytes:
    return bytes([_C1["SYSTEM_SET_PARAM"], t.SYSTEM_INQUIRED_TYPE["ASSIGNABLE_SETTINGS"], len(presets), *presets])


def get_update_param(kind: str) -> bytes:
    return bytes([_C1["UPDT_GET_PARAM"], t.UPDATE_INQUIRED_TYPE[kind]])


def set_fw_update_mode(enabled: bool) -> bytes:
    # The status byte is inverted relative to intuition: ENABLE is 0, DISABLE is 1.
    return bytes([_C1["UPDT_SET_STATUS"], t.UPDATE_INQUIRED_TYPE["FW_UPDATE_MODE"], 0x00 if enabled else 0x01])


# Voice guidance commands travel in table 2 (frame type 0x0E).
_VG_SETTING = 0x01


def vg_get_capability() -> bytes:
    return bytes([_C2["VOICE_GUIDANCE_GET_CAPABILITY"], _VG_SETTING])


def vg_get_onoff() -> bytes:
    return bytes([_C2["VOICE_GUIDANCE_GET_PARAM"], _VG_SETTING, t.VG_DETAILED_TYPE["ON_OFF"]])


def vg_get_language() -> bytes:
    return bytes([_C2["VOICE_GUIDANCE_GET_PARAM"], _VG_SETTING, t.VG_DETAILED_TYPE["LANGUAGE"]])


def vg_set_onoff(on: bool) -> bytes:
    return bytes([_C2["VOICE_GUIDANCE_SET_PARAM"], _VG_SETTING, t.VG_DETAILED_TYPE["ON_OFF"], 1 if on else 0])


def vg_set_update_mode(enabled: bool) -> bytes:
    """Enter/leave language-package update mode. The headset answers with NTFY_STATUS."""
    status = 0x00 if enabled else 0x01  # same inverted meaning as firmware update mode
    return bytes([_C2["VOICE_GUIDANCE_SET_STATUS"], _VG_SETTING, t.VG_STATUS_TYPE["LANGUAGE"], status])


# --- Responses ----------------------------------------------------------------

def parse_protocol_info(p: bytes) -> int:
    return int.from_bytes(p[2:4], "big")


def parse_capability_info(p: bytes) -> dict:
    size = p[3]
    return {"version": p[2], "unique_id": p[4:4 + size].decode("utf-8", "replace")}


def parse_device_string(p: bytes) -> str:
    return p[3:3 + p[2]].decode("utf-8", "replace")


def parse_support_function(p: bytes) -> list[str]:
    return [name_of(t.FUNCTION_TYPE, code) for code in p[3:3 + p[2]]]


def parse_battery(p: bytes) -> dict:
    return {"level": p[2], "charging": name_of(t.BATTERY_CHARGING_STATUS, p[3])}


def parse_ncasm(p: bytes) -> dict:
    return {
        "effect": name_of(t.NCASM_EFFECT, p[2]),
        "nc_setting_type": name_of(t.NCASM_SETTING_TYPE, p[3]),
        "nc_value": p[4],
        "asm_setting_type": name_of(t.ASM_SETTING_TYPE, p[5]),
        "asm_id": name_of(t.ASM_ID, p[6]),
        "asm_value": p[7],
    }


def parse_eq(p: bytes) -> dict:
    count = p[3]
    return {"preset": name_of(t.EQ_PRESET, p[2]), "bands": list(p[4:4 + count])}


def parse_assignable(p: bytes) -> list[str]:
    return [name_of(t.ASSIGNABLE_PRESET, code) for code in p[3:3 + p[2]]]


@dataclass
class AssignablePresetCapability:
    preset: str
    actions: dict[str, str] = field(default_factory=dict)


@dataclass
class AssignableKeyCapability:
    key: str
    key_type: str
    default_preset: str
    presets: list[AssignablePresetCapability] = field(default_factory=list)


def parse_assignable_capability(p: bytes) -> list[AssignableKeyCapability]:
    keys = []
    pos = 3
    for _ in range(p[2]):
        key = AssignableKeyCapability(
            name_of(t.ASSIGNABLE_KEY, p[pos]),
            name_of(t.ASSIGNABLE_KEY_TYPE, p[pos + 1]),
            name_of(t.ASSIGNABLE_PRESET, p[pos + 2]),
        )
        preset_count = p[pos + 3]
        pos += 4
        for _ in range(preset_count):
            preset = AssignablePresetCapability(name_of(t.ASSIGNABLE_PRESET, p[pos]))
            action_count = p[pos + 1]
            pos += 2
            for _ in range(action_count):
                action = name_of(t.ASSIGNABLE_ACTION, p[pos])
                preset.actions[action] = name_of(t.ASSIGNABLE_FUNCTION, p[pos + 1])
                pos += 2
            key.presets.append(preset)
        keys.append(key)
    return keys


GS1, GS2 = 0xD1, 0xD2  # on the XM3: custom button choice, touch panel on/off
GS_BOOLEAN, GS_LIST = 0x01, 0x02
_DISPLAY_ENGLISH = t.MDR_LANGUAGE["ENGLISH"]


def get_general_capability(which: int) -> bytes:
    return bytes([_C1["GENERAL_SETTING_GET_CAPABILITY"], which, _DISPLAY_ENGLISH])


def get_general(which: int) -> bytes:
    return bytes([_C1["GENERAL_SETTING_GET_PARAM"], which])


def set_general(which: int, setting_type: int, value: int) -> bytes:
    return bytes([_C1["GENERAL_SETTING_SET_PARAM"], which, setting_type, value])


def parse_general_capability(p: bytes) -> dict:
    """<cmd> <which> <fmt> <n> title <n> summary <type> [<count> {<fmt> <n> title <n> summary}]"""
    def text(pos: int) -> tuple[str, int]:
        return p[pos + 1:pos + 1 + p[pos]].decode("utf-8", "replace"), pos + 1 + p[pos]

    title, pos = text(3)
    _, pos = text(pos)
    kind = p[pos]
    pos += 1
    options = []
    if kind == GS_LIST:
        count = p[pos]
        pos += 1
        for _ in range(count):
            option, pos = text(pos + 1)  # skips the format byte
            _, pos = text(pos)
            options.append(option)
    return {"title": title, "type": "list" if kind == GS_LIST else "boolean", "options": options}


def parse_general(p: bytes) -> dict:
    return {"type": "list" if p[2] == GS_LIST else "boolean", "value": p[3]}


def parse_vg_capability(p: bytes) -> dict:
    languages = p[5:5 + p[4]] if len(p) > 4 else b""
    return {
        "switch_supported": p[2] == t.VG_SWITCH["SUPPORT"],
        "language_switch_supported": p[3] == t.VG_SWITCH["SUPPORT"],
        "languages": [name_of(t.MDR_LANGUAGE, code) for code in languages],
    }


# Only GET commands, for functions the XM3 lists. Serial number and device-binding queries are left out.
SURVEY = [
    (1, "14 00"), (1, "18 00"), (1, "1C 01"), (1, "1C 02"),
    (1, "36 02"), (1, "36 03"), (1, "36 04"), (1, "36 05"), (1, "36 07"), (1, "36 08"), (1, "36 09"), (1, "36 0A"),
    (1, "40 01"), (1, "40 02"), (1, "42 01"), (1, "42 02"), (1, "46 01"), (1, "46 02"),
    (1, "50 01 01"), (1, "52 01"), (1, "56 01"),
    (1, "60 02"), (1, "62 02"), (1, "66 02"),
    (1, "70 01"),
    (1, "80 01"), (1, "82 01"), (1, "86 01"),
    (1, "A0 01"), (1, "A2 01"), (1, "A6 01"),
    (1, "D0 D1 01"), (1, "D0 D2 01"), (1, "D2 D1"), (1, "D2 D2"), (1, "D6 D1"), (1, "D6 D2"),
    (1, "E0 01"), (1, "E0 02"), (1, "E2 01"), (1, "E2 02"), (1, "E6 01"), (1, "E6 02"),
    (1, "F0 04"), (1, "F2 04"), (1, "F6 04"),
    (2, "40 01"), (2, "42 01 01"), (2, "42 01 02"),
    (2, "46 01 01"), (2, "46 01 02"), (2, "46 01 03"), (2, "46 01 04"), (2, "46 01 05"),
]
