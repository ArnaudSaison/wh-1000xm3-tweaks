"""Sony update-server info.xml codec (info.update.sony.net/<category>/<service>/info/info.xml).
Header 'eaid:ENC0003' = AES-128-ECB, 'daid:HAS0003' = SHA-1; digest = sha1(sha1hex(plain) + service + category).

The AES keys are not shipped with this project. Put them in the XM3_INFOXML_KEYS environment
variable as hex, comma-separated (the first one is used to encrypt). They are published in
lzghzr/MDR_Proxy; see KEY_SOURCE. Getting and using them is at your own risk.

usage: sony_infoxml.py decrypt info.xml HP001 MDRID291600 > plain.xml
       sony_infoxml.py encrypt plain.xml HP001 MDRID291600 > info.xml"""
import hashlib
import os
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_ENV = "XM3_INFOXML_KEYS"
KEY_SOURCE = "https://github.com/lzghzr/MDR_Proxy (file mdr.ts)"


class InfoXmlKeyError(ValueError):
    """The keys are missing, malformed, or none of them opens the file."""


def keys() -> list[bytes]:
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        raise InfoXmlKeyError(f"set {KEY_ENV} to Sony's info.xml AES key(s) in hex, comma-separated; "
                        f"they are published in {KEY_SOURCE}. Use them at your own risk.")
    try:
        found = [bytes.fromhex(k.strip()) for k in raw.split(",") if k.strip()]
    except ValueError:
        raise InfoXmlKeyError(f"{KEY_ENV} must hold hex keys separated by commas") from None
    if not found or any(len(k) != 16 for k in found):
        raise InfoXmlKeyError(f"{KEY_ENV}: every key must be 16 bytes (32 hex characters)")
    return found


def digest(plain, cat, svc):
    return hashlib.sha1((hashlib.sha1(plain).hexdigest() + svc + cat).encode()).hexdigest()


def decrypt(raw, cat, svc):
    hdr, body = raw.split(b"\n\n", 1)
    h = dict(l.split(":", 1) for l in hdr.decode().splitlines())
    for k in keys():
        d = Cipher(algorithms.AES(k), modes.ECB()).decryptor()
        pt = d.update(body) + d.finalize()
        if digest(pt, cat, svc) == h["digest"]:
            return pt
    raise InfoXmlKeyError(f"none of the keys in {KEY_ENV} opens this info.xml")


def encrypt(plain, cat, svc):
    plain += b" " * (-len(plain) % 16)  # Sony pads the XML with spaces to the AES block size
    e = Cipher(algorithms.AES(keys()[0]), modes.ECB()).encryptor()
    return f"eaid:ENC0003\ndaid:HAS0003\ndigest:{digest(plain, cat, svc)}\n\n".encode() + e.update(plain) + e.finalize()


if __name__ == "__main__":
    mode, path, cat, svc = sys.argv[1:5]
    data = open(path, "rb").read()
    try:
        sys.stdout.buffer.write(decrypt(data, cat, svc) if mode == "decrypt" else encrypt(data, cat, svc))
    except InfoXmlKeyError as error:
        raise SystemExit(f"sony_infoxml: {error}")
