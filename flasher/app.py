#!/usr/bin/env python3
"""WH-1000XM3 workbench: a local web UI for status, quiet announcements and uploads.

    uv run flasher/app.py          # http://127.0.0.1:8750

Binds to localhost only. State-changing requests need a per-run token, and only
requests addressed to localhost are served. See flasher/README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO), str(REPO / "tests"), str(REPO / "tools")]

from flask import Flask, Response, abort, jsonify, render_template, request  # noqa: E402

import button_timing  # noqa: E402
import dfu_sign  # noqa: E402
from device import DeviceService, FirmwareImage, discover_paired  # noqa: E402
from xm3.client import MdrError  # noqa: E402
from xm3.package import PackageError, VoicePackage, attenuate, check_firmware, download_package, voice_service_id  # noqa: E402
from xm3.upgrade import UpgradeError  # noqa: E402

CONFIRM_WORD = "UPLOAD"
FIRMWARE_CONFIRM_WORD = "FLASH"
SONY_FIRMWARE = {"MDRID291600": "HDX-2916_V0452_with_stack.bin", "MDRID291601": "HDX-2916_V0452_SPLC_with_stack.bin"}
ERRORS = (MdrError, UpgradeError, PackageError, OSError)


def create_app(data_dir: Path = REPO / "work" / "ui", service: DeviceService | None = None,
               sony_firmware_dir: Path = REPO / "firmware") -> Flask:
    app = Flask(__name__)
    token = secrets.token_urlsafe(16)
    device = service or DeviceService()
    packages_dir = data_dir / "packages"
    config_file = data_dir / "config.json"
    firmware_dir = data_dir / "firmware"  # kept apart, so the voice upload path can never pick one of these
    packages_dir.mkdir(parents=True, exist_ok=True)
    firmware_dir.mkdir(parents=True, exist_ok=True)
    file_checks: dict[tuple[str, str], str | None] = {}  # the signature check takes seconds, and the page polls
    app.config.update(XM3_TOKEN=token, XM3_DEVICE=device)

    def load_config() -> dict:
        try:
            return json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_address(address: str) -> None:
        config_file.write_text(json.dumps({"address": address}), encoding="utf-8")

    def package_entries() -> list[dict]:
        entries = []
        for path in sorted(packages_dir.glob("*.bin"), key=lambda f: f.stat().st_mtime, reverse=True):
            meta_file = path.with_suffix(".json")
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
            entries.append({"id": path.stem, "size": path.stat().st_size, **meta})
        return entries

    def load_package(package_id: str) -> VoicePackage:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", package_id or ""):
            raise PackageError("invalid package ID")
        path = packages_dir / f"{package_id}.bin"
        if not path.is_file():
            raise PackageError(f"no such package: {package_id}")
        return VoicePackage(path.read_bytes())

    def store_package(package_id: str, raw: bytes, **meta) -> dict:
        (packages_dir / f"{package_id}.bin").write_bytes(raw)
        meta["sha1"] = hashlib.sha1(raw).hexdigest()
        meta["created"] = time.strftime("%Y-%m-%d %H:%M")
        (packages_dir / f"{package_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        return {"id": package_id, "size": len(raw), **meta}

    def firmware_entries() -> list[dict]:
        entries = []
        for path in sorted(firmware_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
            entries.append({"id": path.stem, **json.loads(path.read_text(encoding="utf-8"))})
        return entries

    def load_firmware(image_id: str) -> FirmwareImage:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", image_id or ""):
            raise PackageError("invalid firmware ID")
        path = firmware_dir / f"{image_id}.bin"
        if not path.is_file():
            raise PackageError(f"no such firmware file: {image_id}")
        raw = path.read_bytes()
        return FirmwareImage(raw, hashlib.sha1(raw).hexdigest())

    def firmware_file_error(image: FirmwareImage) -> str | None:
        service_id = device.status.get("service", "")
        key = (image.sha1, service_id)
        if key not in file_checks:
            try:
                check_firmware(image.raw, service_id)
                file_checks[key] = None
            except PackageError as err:
                file_checks[key] = str(err)
        return file_checks[key]

    @app.before_request
    def guard():
        # Blocks DNS rebinding: a web page cannot reach this server under another name.
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-Token", ""), token):
            abort(403)

    @app.errorhandler(MdrError)
    @app.errorhandler(UpgradeError)
    @app.errorhandler(PackageError)
    @app.errorhandler(OSError)
    def handle_error(err):
        device.note("error", str(err))
        return jsonify(error=str(err)), 400

    @app.get("/")
    def index():
        return render_template("index.html", token=token, confirm_word=CONFIRM_WORD,
                               firmware_word=FIRMWARE_CONFIRM_WORD)

    @app.get("/api/state")
    def state():
        return jsonify(connected=device.connected, address=device.address or load_config().get("address"),
                       status=device.status, job=device.job, log=list(device.log), packages=package_entries(),
                       firmware=firmware_entries())

    @app.get("/api/paired")
    def paired():
        return jsonify(devices=discover_paired())

    @app.post("/api/connect")
    def connect():
        address = (request.get_json(force=True).get("address") or "").strip().upper()
        if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            raise PackageError("enter a Bluetooth address like AA:BB:CC:DD:EE:FF")
        device.connect(address)
        save_address(address)
        return jsonify(ok=True)

    @app.post("/api/disconnect")
    def disconnect():
        device.disconnect()
        device.note("info", "disconnected")
        return jsonify(ok=True)

    @app.post("/api/refresh")
    def refresh():
        return jsonify(status=device.refresh())

    @app.post("/api/survey")
    def survey():
        return jsonify(rows=device.survey())

    @app.post("/api/test-gaia")
    def test_gaia():
        device.test_gaia()
        return jsonify(ok=True)

    @app.post("/api/download")
    def download():
        language = int(request.get_json(force=True).get("language", 1))
        service = voice_service_id(language)
        package_id = f"sony-{service}"
        path = packages_dir / f"{package_id}.bin"
        device.note("info", f"downloading {service} from Sony...")
        info = download_package(service, path)
        package = VoicePackage(path.read_bytes())
        store_package(package_id, package.raw, kind="sony", source=service, version=info["version"],
                      label=f"Sony original ({service})")
        device.note("ok", f"downloaded and verified {service}, {info['size']} bytes")
        return jsonify(id=package_id)

    @app.post("/api/build")
    def build():
        data = request.get_json(force=True)
        source_id = data.get("source", "")
        source = load_package(source_id)
        voice, beep = int(data.get("voice_steps", 6)), int(data.get("beep_steps", 6))
        raw, report = attenuate(source, voice, beep)
        package_id = f"quiet-v{voice}-b{beep}-{hashlib.sha1(raw).hexdigest()[:8]}"
        label = f"Quiet: voice {report['voice_db']:g} dB, beeps {report['beep_db']:g} dB"
        entry = store_package(package_id, raw, kind="quiet", source=source_id, label=label, **report)
        device.note("ok", f"built {label}, {report['files_changed']} audio blocks changed, "
                          f"{report['clipped_granules']} clipped")
        return jsonify(entry)

    @app.get("/api/files/<package_id>")
    def files(package_id):
        return jsonify(files=load_package(package_id).audio_files)

    @app.get("/api/audio/<package_id>/<path:name>")
    def audio(package_id, name):
        package = load_package(package_id)
        if name not in package.audio_files:
            abort(404)
        return Response(package.read(name), mimetype="audio/mpeg")

    @app.get("/api/preflight")
    def preflight():
        package = None
        package_id = request.args.get("package")
        if package_id:
            try:
                package = load_package(package_id)
            except PackageError:
                pass
        return jsonify(checks=device.preflight(package))

    @app.post("/api/simulate")
    def simulate():
        package_id = request.get_json(force=True).get("package", "")
        device.start_simulation(load_package(package_id), package_id)
        return jsonify(ok=True)

    @app.post("/api/upload")
    def upload():
        data = request.get_json(force=True)
        if data.get("confirm") != CONFIRM_WORD:
            raise PackageError(f"type {CONFIRM_WORD} to confirm")
        package_id = data.get("package", "")
        device.start_upload(load_package(package_id), package_id)
        return jsonify(ok=True)

    @app.post("/api/firmware/build")
    def firmware_build():
        data = request.get_json(force=True)
        values = data.get("timings") or {}
        double = data.get("double_press") is True
        service_id = device.status.get("service", "")
        if service_id not in SONY_FIRMWARE:
            raise PackageError("connect the headset first: its service ID decides which regional file is used")
        source = sony_firmware_dir / SONY_FIRMWARE[service_id]
        if not source.is_file():
            raise PackageError(f"{source.name} is missing: run tools/fetch_firmware.sh")
        try:
            timings = button_timing.normalise(values)
            raw = button_timing.build(source.read_bytes(), timings, double)
            rehearsal = button_timing.rehearse(raw, timings, double)
        except dfu_sign.SignError as err:
            raise PackageError(str(err)) from err
        failed = [row["label"] for row in rehearsal if not row["ok"]]
        if failed:
            raise PackageError(f"not saved: the emulator rehearsal didn't match for {failed[0]}")
        sha1 = hashlib.sha1(raw).hexdigest()
        image_id = f"{'doublepress' if double else 'timing'}-{sha1[:12]}"
        (firmware_dir / f"{image_id}.bin").write_bytes(raw)
        parts = ["double press for pairing"] if double else []
        parts += [f"{c['label'].lower()} {timings[c['id']] / 1000:g} s" for c in button_timing.CONTROLS
                  if timings[c["id"]] != c["stock"]]
        changed = ", ".join(parts)
        meta = {"label": f"{changed[0].upper()}{changed[1:]}, for {service_id}", "sha1": sha1,
                "service": service_id, "timings": timings, "double_press": double}
        (firmware_dir / f"{image_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        device.note("ok", f"built {meta['label']}: offline checks and {len(rehearsal)} emulator rehearsals pass, sha1 {sha1}")
        return jsonify(id=image_id, rehearsal=rehearsal, **meta)

    @app.get("/api/firmware/timings")
    def firmware_timings():
        return jsonify(controls=button_timing.CONTROLS, locked=button_timing.LOCKED,
                       features=button_timing.FEATURES,
                       double_press_window_ms=button_timing.double_press_window())

    @app.post("/api/firmware/check")
    def firmware_check():
        try:
            data = request.get_json(force=True)
            return jsonify(rows=button_timing.check(data.get("timings") or {}, data.get("double_press") is True))
        except dfu_sign.SignError as err:
            raise PackageError(str(err)) from err

    @app.get("/api/firmware/preflight")
    def firmware_preflight():
        image = None
        image_id = request.args.get("id")
        if image_id:
            try:
                image = load_firmware(image_id)
            except PackageError:
                pass
        error = firmware_file_error(image) if image is not None and device.connected else None
        return jsonify(checks=device.preflight_firmware(image, error))

    @app.post("/api/firmware/simulate")
    def firmware_simulate():
        image_id = request.get_json(force=True).get("id", "")
        device.start_firmware_simulation(load_firmware(image_id), image_id)
        return jsonify(ok=True)

    @app.post("/api/firmware/upload")
    def firmware_upload():
        data = request.get_json(force=True)
        if data.get("confirm") != FIRMWARE_CONFIRM_WORD:
            raise PackageError(f"type {FIRMWARE_CONFIRM_WORD} to confirm")
        image_id = data.get("id", "")
        image = load_firmware(image_id)
        device.start_firmware_upload(image, image_id, firmware_file_error(image))
        return jsonify(ok=True)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8750)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = create_app()
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  WH-1000XM3 workbench: {url}\n")
    if not args.no_browser:
        webbrowser.open(url)
    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
