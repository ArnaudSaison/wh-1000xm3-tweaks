"""Command line: uv run python -m xm3 --address AA:BB:CC:DD:EE:FF <command>"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

from . import protocol as p
from . import tables as t
from .client import MdrClient, MdrError
from .package import PackageError, check_firmware, inspect_image
from .transport import GAIA_UUID, MDR_V1_UUID, connect
from .upgrade import Upgrader, UpgradeError, sync_id

MODEL = "WH-1000XM3"
FALLBACK_MIN_BATTERY = 30  # used only if the headset does not report its own threshold
RECONNECT_ATTEMPTS = 6
RECONNECT_DELAY_SECONDS = 5


def cmd_info(client: MdrClient, args) -> None:
    print("protocol version :", hex(client.handshake()))
    print("model            :", client.device_string("MODEL_NAME"))
    print("firmware         :", client.device_string("FW_VERSION"))
    info = p.parse_capability_info(client.request(p.get_capability_info(), "CONNECT_RET_CAPABILITY_INFO"))
    print("capability       :", info)
    functions = p.parse_support_function(client.request(p.get_support_function(), "CONNECT_RET_SUPPORT_FUNCTION"))
    print("functions        :", ", ".join(functions))
    print("battery          :", client.battery())
    if "VOICE_GUIDANCE" in functions:
        reply = client.request(p.vg_get_capability(), "VOICE_GUIDANCE_RET_CAPABILITY", table=2)
        print("voice guidance   :", p.parse_vg_capability(reply))
    for kind in ("UPDATE_METHOD", "BATTERY_POWER_THRESHOLD", "CATEGORY_ID", "SERVICE_ID"):
        try:
            print(f"update {kind:17}:", client.request(p.get_update_param(kind), "UPDT_RET_PARAM").hex(" "))
        except MdrError as err:
            print(f"update {kind:17}: no answer ({err})")


def cmd_survey(client: MdrClient, args) -> None:
    """Send every read-only query above and print the raw replies."""
    client.handshake()
    for table, text in p.SURVEY:
        payload = bytes.fromhex(text)
        client.send(payload, table)
        replies = client.drain(args.listen)
        print(f"T{table} -> {p.command_name(payload, table):32} {payload.hex(' ')}")
        if not replies:
            print("     <- (acknowledged, no reply)")
        for reply_table, reply in replies:
            print(f"     <- {p.command_name(reply, reply_table):32} {reply.hex(' ')}")


def cmd_controls(client: MdrClient, args) -> None:
    """Read-only: the control settings the XM3 exposes, through GENERAL_SETTING."""
    client.handshake()
    for which in (p.GS1, p.GS2):
        info = p.parse_general_capability(client.request(p.get_general_capability(which), "GENERAL_SETTING_RET_CAPABILITY"))
        value = p.parse_general(client.request(p.get_general(which), "GENERAL_SETTING_RET_PARAM"))["value"]
        current = info["options"][value] if info["type"] == "list" and value < len(info["options"]) else value
        print(f"{info['title']}: {current}")
        if info["options"]:
            print("  options:", ", ".join(info["options"]))


def cmd_voice(client: MdrClient, args) -> None:
    client.handshake()
    if args.state:
        client.send(p.vg_set_onoff(args.state == "on"), table=2)
    print("raw on/off reply:", client.request(p.vg_get_onoff(), "VOICE_GUIDANCE_RET_PARAM", table=2).hex(" "))


def cmd_raw(client: MdrClient, args) -> None:
    """Send one payload and print everything received for a few seconds."""
    client.handshake()
    client.send(bytes.fromhex(args.payload), args.table)
    print("sent, listening...")
    for table, payload in client.drain(args.listen):
        print(f"<- {p.command_name(payload, table):32} {payload.hex(' ')}")


def set_update_mode(client: MdrClient, voice: bool, enabled: bool) -> None:
    if voice:
        client.request(p.vg_set_update_mode(enabled), "VOICE_GUIDANCE_NTFY_STATUS", table=2, timeout=10.0)
    else:
        client.set_fw_update_mode(enabled)


def leave_update_mode(client: MdrClient, voice: bool, reconnect, sleep=time.sleep) -> bool:
    """Like the app, leave update mode, and ignore failure.

    The headset reboots during an update, so the original link is normally gone, and the
    command has to go over a new one.
    """
    try:
        set_update_mode(client, voice, False)
        return True
    except (MdrError, OSError):
        pass
    for _ in range(RECONNECT_ATTEMPTS):
        try:
            fresh = MdrClient(reconnect())
        except OSError:
            sleep(RECONNECT_DELAY_SECONDS)
            continue
        try:
            fresh.handshake()
            set_update_mode(fresh, voice, False)
            return True
        except (MdrError, OSError):
            sleep(RECONNECT_DELAY_SECONDS)
        finally:
            fresh.close()
    return False


def cmd_flash(client: MdrClient, args) -> None:
    image = Path(args.image).read_bytes()
    try:
        partitions = inspect_image(image)
    except PackageError as err:
        sys.exit(f"refusing: {err}")
    if args.voice and partitions != [3]:
        sys.exit(f"refusing: --voice needs a package holding only partition 3, this has {partitions}")
    client.handshake()
    model = client.device_string("MODEL_NAME")
    firmware = client.device_string("FW_VERSION")
    service = client.request(p.get_update_param("SERVICE_ID"), "UPDT_RET_PARAM")
    service = service[3:3 + service[2]].decode("ascii", "replace")
    battery = client.battery()["level"]
    threshold = FALLBACK_MIN_BATTERY
    try:
        threshold = client.request(p.get_update_param("BATTERY_POWER_THRESHOLD"), "UPDT_RET_PARAM")[2]
    except MdrError:
        print(f"headset gave no battery threshold, using {FALLBACK_MIN_BATTERY}%")
    print(f"headset : {model}, firmware {firmware}, service {service}, battery {battery}% (must exceed {threshold}%)")
    print(f"image   : {args.image}, {len(image)} bytes, partitions {partitions}")
    print(f"mode    : {'voice guidance package' if args.voice else 'firmware'}")
    print(f"sha1    : {hashlib.sha1(image).hexdigest()}")
    print(f"sync id : {sync_id(image).hex()}")
    if model != MODEL:
        sys.exit(f"refusing: this tool targets {MODEL}, not {model}")
    if not args.voice:
        try:
            check_firmware(image, service)
        except PackageError as err:
            sys.exit(f"refusing: {err}")
        print("checks  : signatures and checksums pass offline, and the region matches")
    if battery <= threshold:
        sys.exit("refusing: battery too low")
    if not args.really_flash:
        print("dry run: nothing was changed. Pass --really-flash to write this image.")
        return
    set_update_mode(client, args.voice, True)
    try:
        last = {"percent": -1}

        def progress(done: int, total: int) -> None:
            percent = done * 100 // total
            if percent != last["percent"]:
                last["percent"] = percent
                print(f"\r{percent:3}%", end="", flush=True)

        with client.serviced_in_background():
            Upgrader(image, lambda: connect(args.address, GAIA_UUID), progress=progress).run()
    finally:
        def reconnect():
            return connect(args.address, MDR_V1_UUID, args.channel)

        if not leave_update_mode(client, args.voice, reconnect):
            print("\ncould not send leave-update-mode: the headset may need a power cycle")


def main() -> int:
    parser = argparse.ArgumentParser(prog="xm3", description=__doc__)
    parser.add_argument("--address", required=True, help="headset Bluetooth address")
    parser.add_argument("--channel", type=int, help="RFCOMM channel for MDR, skipping SDP")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="read-only device summary").set_defaults(run=cmd_info)
    survey = sub.add_parser("survey", help="read-only: query every supported feature, print raw replies")
    survey.add_argument("--listen", type=float, default=0.8)
    survey.set_defaults(run=cmd_survey)
    sub.add_parser("controls", help="read-only: custom button and touch panel settings").set_defaults(run=cmd_controls)
    voice = sub.add_parser("voice", help="show or set voice guidance")
    voice.add_argument("state", nargs="?", choices=["on", "off"])
    voice.set_defaults(run=cmd_voice)
    raw = sub.add_parser("raw", help="send one hex payload and print the replies")
    raw.add_argument("payload")
    raw.add_argument("--table", type=int, choices=[1, 2], default=1)
    raw.add_argument("--listen", type=float, default=3.0)
    raw.set_defaults(run=cmd_raw)
    flash = sub.add_parser("flash", help="write an update file (dry run unless --really-flash)")
    flash.add_argument("image")
    flash.add_argument("--voice", action="store_true", help="the file is a voice guidance package, not firmware")
    flash.add_argument("--really-flash", action="store_true", help="actually write; can brick the headset")
    flash.set_defaults(run=cmd_flash)
    args = parser.parse_args()
    try:
        client = MdrClient(connect(args.address, MDR_V1_UUID, args.channel))
    except OSError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    try:
        args.run(client, args)
    except (MdrError, UpgradeError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
