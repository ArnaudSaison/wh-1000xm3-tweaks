"""Rebuild a WH-1000XM3 upgrade file (APPUHDR2) with files replaced inside the partition-3 'File' images.
Replacement files must keep the exact original size (in-place patch); the XOR-16 checksum word of the
image is then corrected. usage:
  repack.py original.bin output.bin [fsindex:path/in/fs=replacement_file ...]
  fsindex = 1 (prompts), 2 (tables/ANC), 3 (DSP .kap)"""
import sys, struct, hashlib
src = open(sys.argv[1], "rb").read()
chunks = []; o = 0
while o < len(src):
    tag = src[o:o+8]; ln, = struct.unpack(">I", src[o+8:o+12]); chunks.append([tag, bytearray(src[o+12:o+12+ln])]); o += 12 + ln
assert o == len(src)
def fs_index(body):  # PARTDATA body: u16 partition | u16 sub | image
    return struct.unpack(">HH", body[:4])
def entries(img):
    n, = struct.unpack(">H", img[8:10])
    ents = [struct.unpack(">III", img[10+12*i:22+12*i]) for i in range(n)]
    def name(off):
        l, = struct.unpack(">H", img[2*off:2*off+2]); return img[2*off+2:2*off+2+2*l].decode("utf-16-be")
    out = {}
    def walk(i, path):
        no, a, b = ents[i-1]; nm = "" if i == 1 else name(no & 0x3fffffff); p = f"{path}/{nm}".lstrip("/")
        if no & 0x80000000:
            for c in range(a, a+b): walk(c, p)
        else: out[p] = (2*a, b)
    walk(1, ""); return out
def fix_xor(img):
    x = 0
    for i in range(0, len(img), 2): x ^= struct.unpack(">H", img[i:i+2])[0]
    ck = len(img) - 12  # trailer: checksum u16 | 0x0100 | u32 size_words | "File"
    old, = struct.unpack(">H", img[ck:ck+2]); img[ck:ck+2] = struct.pack(">H", old ^ x); return old, old ^ x
mods = {}
for spec in sys.argv[3:]:
    left, repl = spec.split("=", 1); idx, path = left.split(":", 1); mods.setdefault(int(idx), []).append((path, repl))
for tag, body in chunks:
    if tag != b"PARTDATA": continue
    part, sub = fs_index(body)
    if part != 3 or sub not in mods: continue
    img = bytearray(body[4:])
    table = entries(img); seen = {}
    for path, repl in mods[sub]:
        new = open(repl, "rb").read(); off, size = table[path]
        assert len(new) == size, f"{path}: size {len(new)} != {size}"
        if off in seen:  # several names can share the same data (dedup), make that visible
            print(f"  note: {path} shares data with {seen[off]}")
        seen[off] = path; img[off:off+size] = new
    old, new = fix_xor(img); body[4:] = img
    print(f"fs{sub}: patched {len(mods[sub])} file(s), checksum {old:#06x} -> {new:#06x}")
out = b"".join(t + struct.pack(">I", len(b)) + bytes(b) for t, b in chunks)
open(sys.argv[2], "wb").write(out)
print(f"wrote {sys.argv[2]} size={len(out)} sha1={hashlib.sha1(out).hexdigest()}")
