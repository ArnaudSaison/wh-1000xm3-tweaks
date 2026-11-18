#!/usr/bin/env python3
"""Check signatures in a WH-1000XM3 update file, and patch vm.app without breaking them.

    uv run tools/dfu_sign.py verify  firmware.bin
    uv run tools/dfu_sign.py where   firmware.bin 0x3772D
    uv run tools/dfu_sign.py patch   firmware.bin out.bin 0x3772D=200
    uv run tools/dfu_sign.py pairing firmware.bin out.bin --ms 3000

The scheme, found in the stack firmware (see docs/firmware-analysis.md):

* RSA-1024 with e=3 and no padding. The signature cubed must equal a 1024-bit digest.
* The digest is 8 MD5 chains side by side, mixed one 32-word block at a time.
* Each block feeds MD5 from overlapping 16-bit words, so only words 0-16 of each
  32-word block affect the digest. Words 17-31 are not covered by the signature.

`patch` only ever writes uncovered words. It keeps vm.app's and the filesystem's XOR
checks valid, and keeps the covered filesystem CRC valid using three words inside a
debug string. It then re-verifies its own output and refuses to write if that fails.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

M32 = 0xFFFFFFFF
_K = [int(abs(math.sin(i + 1)) * 2**32) & M32 for i in range(64)]
_S = [7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4
_G = [*range(16), *[(5 * i + 1) % 16 for i in range(16, 32)], *[(3 * i + 5) % 16 for i in range(32, 48)],
      *[(7 * i) % 16 for i in range(48, 64)]]
_IV = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
BLOCK_WORDS = 32
COVERED_PER_BLOCK = 17  # words 0..16
RSA_WORDS = 64
FS_MAGIC = b"fsr_dfu1"
FS_SIG_WORD = 4  # the signature follows the 4-word magic
FS_HASH_START = FS_SIG_WORD + RSA_WORDS  # covers the CRC field and everything after it, except the last word
FS_IMAGE_START = FS_HASH_START + 2
FS_IMAGE_CHECKSUM_FROM_END = 6
VM_CHECKSUM_WORD = 0x13
PATCH_STRING_PREFIX = "platform/csr867x/"
FILLER_WORDS = 3
VM_DATA_DELTA = 0x2E943  # vm.app file word minus data address
PAIRING_TABLE = 0x37721
PAIRING_DESCRIPTOR = 0x3776E
PAIRING_STRING = 0x394E4
PAIRING_STRING_TEXT = "platform/csr867x/dsp_audio.c"
PAIRING_NEW_TABLE = 0x394E9
MIN_PAIRING_MS = 2000  # above the 1.5 s entry in the same table
MAX_PAIRING_MS = 15000


class SignError(ValueError):
    pass


def _compress(state: list[int], offset: int, m: list[int]) -> None:
    a, b, c, d = state[offset:offset + 4]
    a0, b0, c0, d0 = a, b, c, d
    for i in range(64):
        if i < 16:
            f = (b & c) | (~b & d)
        elif i < 32:
            f = (d & b) | (~d & c)
        elif i < 48:
            f = b ^ c ^ d
        else:
            f = c ^ (b | (~d & M32))
        t = (a + (f & M32) + _K[i] + m[_G[i]]) & M32
        s = _S[i]
        a, d, c, b = d, c, b, (b + (((t << s) | (t >> (32 - s))) & M32)) & M32
    state[offset:offset + 4] = [(a0 + a) & M32, (b0 + b) & M32, (c0 + c) & M32, (d0 + d) & M32]


def digest(words: list[int]) -> list[int]:
    """The 64-word digest. Note the overlapping fetch: m[g] uses words g and g+1."""
    state = [_IV[i % 4] for i in range(32)]
    index = len(words) & (BLOCK_WORDS - 1)
    bits = len(words) * 16
    padding = (0x1C if index < 0x1C else 0x3C) - index
    data = [*words, 0x80, *[0] * (padding - 1), bits & 0xFFFF, (bits >> 16) & 0xFFFF, 0, 0, 0]
    offset = 0
    for block in range(0, len(data) - 1, BLOCK_WORDS):
        _compress(state, offset, [data[block + g] | data[block + g + 1] << 16 for g in range(16)])
        offset = 0 if offset + 4 > 0x1F else offset + 4
    out: list[int] = []
    for value in state:
        out += [value >> 16, value & 0xFFFF]
    out[0] &= 0x7FFF
    return out


def words_le(data: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(data) // 2}H", data[:len(data) // 2 * 2]))


def cube(signature: list[int], modulus: int) -> list[int]:
    value = pow(int("".join(f"{w:04x}" for w in signature), 16), 3, modulus)
    return [(value >> (16 * (RSA_WORDS - 1 - i))) & 0xFFFF for i in range(RSA_WORDS)]


def is_covered(section_position: int) -> bool:
    """Whether a word at this offset from the start of the hashed data is signed."""
    return section_position % BLOCK_WORDS < COVERED_PER_BLOCK


@dataclass
class Sections:
    """Offsets inside one CSR-dfu2 file. Layout follows tools/csrdfu.py."""
    dfu_start: int
    stack: tuple[int, int] | None
    sps: tuple[int, int]
    aps: tuple[int, int]
    fs: tuple[int, int]
    file_crc: int


def _partition_one(image: bytes) -> tuple[int, int]:
    position = 0
    while position < len(image):
        tag = image[position:position + 8]
        size = int.from_bytes(image[position + 8:position + 12], "big")
        if tag == b"PARTDATA" and image[position + 12:position + 14] == b"\x00\x01":
            return position + 14, size - 2
        position += 12 + size
    raise SignError("no partition 1 in this file: nothing here is signed")


def parse_sections(image: bytes) -> Sections:
    start, size = _partition_one(image)
    if image[start:start + 2] == b"\x00\x00":  # the sub-image number
        start, size = start + 2, size - 2
    d = image[start:start + size]
    if d[:8] != b"CSR-dfu2":
        raise SignError("partition 1 is not a CSR-dfu2 file")
    desc_len = struct.unpack_from("<I", d, 100)[0]
    position = 104
    stack_lengths = [struct.unpack_from("<QQI", d, position + 20 * i)[2] for i in range(16)]
    position += 320 + 224
    ps_entries = [struct.unpack_from("<QIII", d, position + 20 * i) for i in range(16)]
    position += 320
    fs_entries = [struct.unpack_from("<IHHHHI", d, position + 16 * i) for i in range(16)]
    position += 256 + desc_len
    if sum(1 for n in stack_lengths if n) > 1 or sum(1 for _, a, s, b in ps_entries if a) \
            or sum(1 for e in ps_entries if e[2]) != 1 or sum(1 for e in ps_entries if e[3]) != 1 \
            or sum(1 for e in fs_entries if e[5]) != 1:
        raise SignError("unexpected section layout: refusing to guess")
    stack = None
    if stack_lengths[0]:
        stack = (start + position, stack_lengths[0])
        position += stack_lengths[0]
    sps_len = next(e[2] for e in ps_entries if e[2])
    aps_len = next(e[3] for e in ps_entries if e[3])
    fs_len = next(e[5] for e in fs_entries if e[5])
    sps = (start + position, sps_len)
    aps = (start + position + sps_len, aps_len)
    fs = (start + position + sps_len + aps_len, fs_len)
    return Sections(start, stack, sps, aps, fs, fs[0] + fs_len)


def read_ps_keys(image: bytes, section: tuple[int, int]) -> dict[int, list[int]]:
    """The PS keys in one settings section: Sections.sps or Sections.aps."""
    blob = image[section[0]:section[0] + section[1]]
    _, count = struct.unpack_from("<HH", blob, 0)
    position = 4
    keys: dict[int, list[int]] = {}
    for _ in range(count):
        _, key, length = struct.unpack_from("<HHH", blob, position)
        position += 6
        keys[key] = list(struct.unpack_from(f"<{length}H", blob, position))
        position += 2 * length
    return keys


def read_modulus(image: bytes, sections: Sections) -> int:
    """PS keys 350 and 351 are the two halves of N."""
    keys = read_ps_keys(image, sections.sps)
    if 350 not in keys or 351 not in keys:
        raise SignError("this file does not contain the RSA key")
    return int("".join(f"{w:04x}" for w in keys[350] + keys[351]), 16)


def verify(image: bytes, modulus: int | None = None) -> dict[str, bool]:
    s = parse_sections(image)
    n = modulus or read_modulus(image, s)
    results = {}
    for name, (start, length) in (("stack settings", s.sps), ("app settings", s.aps)):
        words = words_le(image[start:start + length])
        results[name] = digest(words[:-(RSA_WORDS + 2)]) == cube(words[-(RSA_WORDS + 2):-2], n)
    start, length = s.fs
    blob = image[start:start + length]
    words = words_le(blob)
    results["filesystem"] = digest(words[FS_HASH_START:-1]) == cube(words[FS_SIG_WORD:FS_HASH_START], n)
    results["filesystem crc"] = _crc(blob[2 * FS_HASH_START:]) == 0
    results["filesystem xor"] = _xor(words[FS_IMAGE_START:]) == 0
    dfu = image[s.dfu_start:s.file_crc]
    results["file crc"] = _crc(dfu) == struct.unpack_from("<I", image, s.file_crc)[0]
    return results


def _crc(data: bytes) -> int:
    return zlib.crc32(data) ^ M32


def _xor(words: list[int]) -> int:
    value = 0
    for word in words:
        value ^= word
    return value


class VmLayout:
    """Where vm.app sits in the filesystem, from its big-endian 'BIGvmApp' magic."""

    def __init__(self, words: list[int]):
        magic = [0x4249, 0x4776, 0x6D41, 0x7070]
        for i in range(FS_IMAGE_START, len(words) - 4):
            if words[i:i + 4] == magic:
                self.start = i
                break
        else:
            raise SignError("vm.app not found in the filesystem")
        self.end = len(words) - 12  # the file sits directly before the 12-word image trailer

    def covered(self, vm_word: int) -> bool:
        return is_covered(self.start + vm_word - FS_HASH_START)


def _text_at(words: list[int], index: int) -> str:
    text = ""
    while 32 <= words[index] < 127:
        text += chr(words[index])
        index += 1
    return text


def _find_compensation(words: list[int], layout: VmLayout) -> int:
    """Three adjacent uncovered words inside a debug source-path string in vm.app."""
    prefix = [ord(c) for c in PATCH_STRING_PREFIX]
    i = layout.start
    while i < layout.end:
        if words[i:i + len(prefix)] == prefix and _text_at(words, i).endswith(".c"):
            j = i + len(prefix)
            while all(32 <= words[j + k] < 127 for k in range(FILLER_WORDS)):
                if not any(is_covered(j + k - FS_HASH_START) for k in range(FILLER_WORDS)):
                    return j
                j += 1
            i = j
        i += 1
    raise SignError("no harmless place found for the CRC correction")


def _solve_filler(blob: bytearray, position: int) -> None:
    """Choose the filler words so the CRC is valid and the XOR checks stay unchanged.

    CRC and XOR are both linear over GF(2), so this is a small system of linear equations:
    48 unknown bits, 32 equations for the CRC and 16 for the XOR.
    """
    region = FS_HASH_START * 2
    base = zlib.crc32(bytes(blob[region:]))
    bits = FILLER_WORDS * 16
    rows = []
    for bit in range(bits):
        byte, shift = position + bit // 8, bit % 8
        blob[byte] ^= 1 << shift
        crc_effect = zlib.crc32(bytes(blob[region:])) ^ base
        blob[byte] ^= 1 << shift
        xor_effect = 1 << (bit % 16)
        rows.append((crc_effect | xor_effect << 32, 1 << bit))
    want = base ^ M32  # a valid section leaves the CRC state at all ones
    solved = []
    for pivot in range(bits):
        mask = 1 << pivot
        found = next((row for row in rows if row[0] & mask), None)
        if found is None:
            continue
        rows.remove(found)
        rows = [(a ^ found[0], b ^ found[1]) if a & mask else (a, b) for a, b in rows]
        solved = [(a ^ found[0], b ^ found[1]) if a & mask else (a, b) for a, b in solved]
        solved.append(found)
    choice = 0
    for value, flips in solved:
        if want & (value & -value):
            want ^= value
            choice ^= flips
    if want:
        raise SignError("no valid CRC correction exists at this position")
    for i in range(FILLER_WORDS * 2):
        blob[position + i] ^= (choice >> (8 * i)) & 0xFF


def patch(image: bytes, edits: dict[int, int]) -> bytes:
    """Apply vm.app word edits. Raises SignError rather than breaking a signature."""
    s = parse_sections(image)
    modulus = read_modulus(image, s)
    if not all(verify(image, modulus).values()):
        raise SignError("the input file does not verify, so a patch could not be trusted")
    start, length = s.fs
    blob = bytearray(image[start:start + length])
    words = words_le(bytes(blob))
    layout = VmLayout(words)
    for vm_word, value in edits.items():
        if not 0 <= value <= 0xFFFF:
            raise SignError(f"value {value} does not fit in a word")
        if vm_word == VM_CHECKSUM_WORD or not 0 <= vm_word < layout.end - layout.start:
            raise SignError(f"word {vm_word:#x} cannot be edited")
        if layout.covered(vm_word):
            raise SignError(f"word {vm_word:#x} is covered by the signature")

    def put(index: int, value: int) -> None:
        struct.pack_into("<H", blob, 2 * index, value)
        words[index] = value

    for vm_word, value in edits.items():
        put(layout.start + vm_word, value)
    filler = _find_compensation(words, layout)
    check_at = layout.start + VM_CHECKSUM_WORD
    image_check_at = len(words) - FS_IMAGE_CHECKSUM_FROM_END
    for protected in (check_at, image_check_at):
        if is_covered(protected - FS_HASH_START):
            raise SignError("a checksum word falls in a signed position in this file")

    put(check_at, words[check_at] ^ _xor(words[layout.start:layout.end]))
    put(image_check_at, words[image_check_at] ^ _xor(words[FS_IMAGE_START:]))
    _solve_filler(blob, 2 * filler)
    output = bytearray(image)
    output[start:start + length] = blob
    struct.pack_into("<I", output, s.file_crc, _crc(bytes(output[s.dfu_start:s.file_crc])))
    result = bytes(output)
    outcome = verify(result, modulus)
    if not all(outcome.values()):
        raise SignError(f"patched file failed verification, not writing it: {outcome}")
    return result


def pairing_edits(image: bytes, milliseconds: int, short_ms: int = 1500) -> dict[int, int]:
    """Edits giving the pairing hold, and optionally the short hold, new durations, for firmware 4.5.2.

    The original 7 s duration words are signed, but input 1's table pointer is not.
    So a copy of its table goes into uncovered words inside a debug string, and the
    pointer moves to the copy. Every user of that string passes it to the assert helper
    0x1BA73, which never reads it. See docs/firmware-analysis.md for what the event does.
    """
    if not MIN_PAIRING_MS <= milliseconds <= MAX_PAIRING_MS:
        raise SignError(f"pairing hold must be {MIN_PAIRING_MS} to {MAX_PAIRING_MS} ms")
    s = parse_sections(image)
    words = words_le(image[s.fs[0]:s.fs[0] + s.fs[1]])
    layout = VmLayout(words)
    vm = words[layout.start:layout.end]
    expected = [1500, 0, 0x13, 1500, 1, 0x14, 7000, 0, 0x15, 7000, 1, 0x16]
    if vm[PAIRING_TABLE:PAIRING_TABLE + 12] != expected or vm[PAIRING_DESCRIPTOR + 1] != 4:
        raise SignError("this is not the expected firmware: the button table differs")
    if vm[PAIRING_DESCRIPTOR] != (PAIRING_TABLE - VM_DATA_DELTA) & 0xFFFF:
        raise SignError("this is not the expected firmware: the table pointer differs")
    if _text_at(vm, PAIRING_STRING) != PAIRING_STRING_TEXT or PAIRING_NEW_TABLE + len(expected) > \
            PAIRING_STRING + len(PAIRING_STRING_TEXT):
        raise SignError("this is not the expected firmware: the debug string differs")
    if short_ms >= milliseconds:
        raise SignError("the short hold must end before the pairing hold")
    table = [short_ms, 0, 0x13, short_ms, 1, 0x14, milliseconds, 0, 0x15, milliseconds, 1, 0x16]
    edits = {PAIRING_NEW_TABLE + i: value for i, value in enumerate(table)}
    edits[PAIRING_DESCRIPTOR] = (PAIRING_NEW_TABLE - VM_DATA_DELTA) & 0xFFFF
    return edits


def parse_edit(text: str) -> tuple[int, int]:
    word, value = text.split("=", 1)
    return int(word, 0), int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    verify_cmd = sub.add_parser("verify")
    verify_cmd.add_argument("file", type=Path)
    where = sub.add_parser("where")
    where.add_argument("file", type=Path)
    where.add_argument("words", nargs="+", type=lambda t: int(t, 0))
    patch_cmd = sub.add_parser("patch")
    patch_cmd.add_argument("file", type=Path)
    patch_cmd.add_argument("output", type=Path)
    patch_cmd.add_argument("edits", nargs="+", type=parse_edit, metavar="WORD=VALUE")
    pairing = sub.add_parser("pairing", help="build firmware with a different pairing hold time")
    pairing.add_argument("file", type=Path)
    pairing.add_argument("output", type=Path)
    pairing.add_argument("--ms", type=int, required=True)
    args = parser.parse_args()
    image = args.file.read_bytes()
    try:
        if args.command == "verify":
            results = verify(image)
            for name, ok in results.items():
                print(f"{name:16} {'OK' if ok else 'FAILED'}")
            return 0 if all(results.values()) else 1
        s = parse_sections(image)
        words = words_le(image[s.fs[0]:s.fs[0] + s.fs[1]])
        layout = VmLayout(words)
        if args.command == "where":
            for word in args.words:
                state = "covered by the signature" if layout.covered(word) else "NOT covered"
                print(f"{word:#07x} value {words[layout.start + word]:#06x}: {state}")
            return 0
        edits = pairing_edits(image, args.ms) if args.command == "pairing" else dict(args.edits)
        output = patch(image, edits)
        args.output.write_bytes(output)
        changed = sum(a != b for a, b in zip(image, output))
        print(f"wrote {args.output}: {changed} bytes changed, signatures still verify")
        print(f"sha1 {hashlib.sha1(output).hexdigest()}")
        return 0
    except SignError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
