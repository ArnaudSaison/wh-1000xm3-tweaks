"""Read, check, attenuate and download APPUHDR2 update packages.

Pure file handling: nothing here talks to a headset.
"""
from __future__ import annotations

import hashlib
import re
import struct
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import dfu_sign  # noqa: E402
from mp3gain_lite import patch as attenuate_mp3  # noqa: E402

VARIANT = b"ZARKOV77"
SONY_UPDATE_HOST = "http://info.update.sony.net"
CATEGORY = "HP001"
STEP_DB = 1.5
REGION_PS_KEY = 698  # the setting that tells the regional files apart (meaning inferred)
REGION_VALUES = {"MDRID291600": [0x0000, 0x0000], "MDRID291601": [0x0001, 0xFFFF]}


class PackageError(ValueError):
    pass


@dataclass
class Chunk:
    tag: bytes
    body: bytes


def split_chunks(image: bytes) -> list[Chunk]:
    chunks = []
    pos = 0
    while pos < len(image):
        if pos + 12 > len(image):
            raise PackageError("truncated chunk header")
        tag, size = image[pos:pos + 8], int.from_bytes(image[pos + 8:pos + 12], "big")
        if pos + 12 + size > len(image):
            raise PackageError(f"chunk {tag!r} runs past the end of the file")
        chunks.append(Chunk(tag, image[pos + 12:pos + 12 + size]))
        pos += 12 + size
    return chunks


def join_chunks(chunks: list[Chunk]) -> bytes:
    return b"".join(c.tag + len(c.body).to_bytes(4, "big") + c.body for c in chunks)


def inspect_image(image: bytes) -> list[int]:
    """Check the container and return the partition number of each PARTDATA chunk."""
    if image[:8] != b"APPUHDR2":
        raise PackageError("not an APPUHDR2 update file")
    chunks = split_chunks(image)
    if chunks[0].body[:8] != VARIANT:
        raise PackageError(f"wrong variant {chunks[0].body[:8]!r}, expected {VARIANT!r}")
    if chunks[-1].tag != b"APPUPFTR" or len(chunks[-1].body) != 128:
        raise PackageError("missing or malformed 128-byte footer")
    return [int.from_bytes(c.body[:2], "big") for c in chunks if c.tag == b"PARTDATA"]


def check_firmware(image: bytes, service_id: str) -> None:
    """Refuse firmware whose signatures fail, or which is for another region.

    A local check only: the headset applies its own, and passing here does not prove it
    will accept the file.
    """
    if service_id not in REGION_VALUES:
        raise PackageError(f"unknown service ID {service_id!r}: refusing to guess its region")
    try:
        results = dfu_sign.verify(image)
        region = dfu_sign.read_ps_keys(image, dfu_sign.parse_sections(image).aps).get(REGION_PS_KEY)
    except (dfu_sign.SignError, struct.error, IndexError) as err:
        raise PackageError(f"cannot read firmware sections: {err}") from err
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        raise PackageError("signature or checksum check failed: " + ", ".join(failed))
    if region != REGION_VALUES[service_id]:
        raise PackageError(f"this file is not for {service_id}: wrong region")


def file_table(image: bytes) -> dict[str, tuple[int, int]]:
    """Map path -> (offset, size) inside a big-endian CSR read-only 'File' image."""
    if image[:4] != b"File":
        raise PackageError("not a File image")
    count = struct.unpack(">H", image[8:10])[0]
    entries = [struct.unpack(">III", image[10 + 12 * i:22 + 12 * i]) for i in range(count)]

    def name(offset: int) -> str:
        length = struct.unpack(">H", image[2 * offset:2 * offset + 2])[0]
        return image[2 * offset + 2:2 * offset + 2 + 2 * length].decode("utf-16-be")

    table: dict[str, tuple[int, int]] = {}

    def walk(index: int, path: str) -> None:
        name_field, first, second = entries[index - 1]
        current = "" if index == 1 else name(name_field & 0x3FFFFFFF)
        full = f"{path}/{current}".lstrip("/")
        if name_field & 0x80000000:
            for child in range(first, first + second):
                walk(child, full)
        else:
            table[full] = (2 * first, second)

    walk(1, "")
    return table


def xor16(image: bytes) -> int:
    value = 0
    for i in range(0, len(image) - len(image) % 2, 2):
        value ^= (image[i] << 8) | image[i + 1]
    return value


def _fix_checksum(image: bytearray) -> None:
    """The checksum word sits 12 bytes from the end. A valid image XORs to zero."""
    position = len(image) - 12
    old = (image[position] << 8) | image[position + 1]
    image[position:position + 2] = (old ^ xor16(image)).to_bytes(2, "big")


@dataclass
class VoicePackage:
    """A package holding only the announcements partition (partition 3, sub-image 1)."""
    raw: bytes
    chunks: list[Chunk] = field(init=False)
    image: bytes = field(init=False)
    files: dict[str, tuple[int, int]] = field(init=False)

    def __post_init__(self) -> None:
        partitions = inspect_image(self.raw)
        if partitions != [3]:
            raise PackageError(f"not a voice package: it holds partitions {partitions}, expected only [3]")
        self.chunks = split_chunks(self.raw)
        body = next(c.body for c in self.chunks if c.tag == b"PARTDATA")
        if body[:4] != b"\x00\x03\x00\x01":
            raise PackageError(f"expected partition 3 sub-image 1, found {body[:4].hex()}")
        self.image = body[4:]
        self.files = file_table(self.image)
        if xor16(self.image) != 0:
            raise PackageError("image checksum is invalid")

    def read(self, path: str) -> bytes:
        offset, size = self.files[path]
        return self.image[offset:offset + size]

    @property
    def language(self) -> int | None:
        return self.read("voice/language.bin")[0] if "voice/language.bin" in self.files else None

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.raw).hexdigest()

    @property
    def audio_files(self) -> list[str]:
        return sorted(p for p in self.files if p.endswith(".mp3"))


def rebuild_package(package: VoicePackage, image: bytearray, changed_ranges: list[tuple[int, int]]) -> bytes:
    """Fix the checksum and return the package, refusing changes outside the given ranges."""
    if len(image) != len(package.image):
        raise PackageError("internal error: the image changed size")
    _fix_checksum(image)
    checksum_at = len(image) - 12
    allowed = changed_ranges + [(checksum_at, checksum_at + 2)]
    for i, (old, new) in enumerate(zip(package.image, image)):
        if old != new and not any(start <= i < end for start, end in allowed):
            raise PackageError(f"internal error: unexpected change at offset {i:#x}")
    chunks = [Chunk(c.tag, c.body[:4] + bytes(image) if c.tag == b"PARTDATA" else c.body) for c in package.chunks]
    raw = join_chunks(chunks)
    VoicePackage(raw)  # re-parse: must still be a valid package
    return raw


def attenuate(package: VoicePackage, voice_steps: int, beep_steps: int) -> tuple[bytes, dict]:
    """Return a new package with lowered announcements, plus a report.

    Files are patched in place at the same size, so nothing else moves. The result
    is verified to differ from the source only inside audio files and the checksum.
    """
    if not (0 <= voice_steps <= 40 and 0 <= beep_steps <= 40):
        raise PackageError("attenuation must be between 0 and 40 steps")
    image = bytearray(package.image)
    done: set[int] = set()
    changed_ranges: list[tuple[int, int]] = []
    clipped = 0
    for path in package.audio_files:
        if Path(path).name.startswith("silent_"):
            continue
        steps = voice_steps if path.startswith("voice/") else beep_steps
        offset, size = package.files[path]
        if offset in done or steps == 0:  # several names can share the same data
            continue
        done.add(offset)
        patched, granules, clipped_here = attenuate_mp3(package.read(path), steps)
        if len(patched) != size or granules == 0:
            raise PackageError(f"{path}: attenuation failed ({granules} granules)")
        clipped += clipped_here
        image[offset:offset + size] = patched
        changed_ranges.append((offset, offset + size))
    raw = rebuild_package(package, image, changed_ranges)
    return raw, {
        "voice_db": -voice_steps * STEP_DB,
        "beep_db": -beep_steps * STEP_DB,
        "files_changed": len(done),
        "clipped_granules": clipped,
    }


def voice_service_id(language: int) -> str:
    return f"VGIDCSR00{language:02X}"


def download_package(service: str, destination: Path) -> dict:
    """Fetch a package from Sony's server and verify it against the SHA-1 in its manifest."""
    if not re.fullmatch(r"[A-Z0-9]{6,16}", service):
        raise PackageError(f"invalid service ID {service!r}")
    import sony_infoxml  # needs the cryptography package, so imported only when used

    base = f"{SONY_UPDATE_HOST}/{CATEGORY}/{service}"
    try:
        sony_infoxml.keys()  # fail with a clear message before touching the network
        with urllib.request.urlopen(f"{base}/info/info.xml", timeout=30) as reply:
            raw = reply.read()
        manifest = sony_infoxml.decrypt(raw, CATEGORY, service).decode("utf-8", "replace")
    except sony_infoxml.InfoXmlKeyError as error:
        raise PackageError(str(error)) from None
    match = re.search(r'<Distribution [^>]*ID="FW"[^>]*>', manifest)
    if not match:
        raise PackageError("manifest has no firmware entry")
    attributes = dict(re.findall(r'(\w+)="([^"]*)"', match[0]))
    if not attributes["URI"].startswith(f"{SONY_UPDATE_HOST}/"):
        raise PackageError(f"unexpected download host in {attributes['URI']}")
    with urllib.request.urlopen(attributes["URI"], timeout=120) as reply:
        data = reply.read()
    digest = hashlib.sha1(data).hexdigest()
    if digest != attributes["MAC"].lower() or len(data) != int(attributes["Size"]):
        raise PackageError(f"download failed verification: sha1 {digest}, expected {attributes['MAC']}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return {"sha1": digest, "size": len(data), "version": attributes.get("Version", "?")}
