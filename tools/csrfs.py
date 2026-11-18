"""Extract a CSR BlueCore read-only filesystem image ("File" magic, big-endian, word addressed).
Layout (offsets in 16-bit words from the 'File' magic):
  'File' | u32 size_words | u16 n_entries | n * entry(12 bytes) | names (u16 len + UTF-16BE) | data
  entry: u32 name_off (bit31 = directory, bit30 = unknown flag, seen on .kap files) | u32 data_off or first_child (1-based) | u32 size_bytes or n_children
usage: csrfs.py image.bin outdir [skip_prefix_bytes]"""
import sys, struct, pathlib
d = pathlib.Path(sys.argv[1]).read_bytes()[int(sys.argv[3]) if len(sys.argv) > 3 else 2:]
assert d[:4] == b"File", d[:8]
size_w, n = struct.unpack(">IH", d[4:10])
ents = [struct.unpack(">III", d[10+12*i:22+12*i]) for i in range(n)]
def name(off):
    ln, = struct.unpack(">H", d[2*off:2*off+2]); return d[2*off+2:2*off+2+2*ln].decode("utf-16-be")
out = pathlib.Path(sys.argv[2]); listing = []
def walk(idx, path):
    no, a, b = ents[idx-1]
    nm = "" if idx == 1 else name(no & 0x3fffffff)
    p = path / nm
    if no & 0x80000000:
        p.mkdir(parents=True, exist_ok=True)
        for c in range(a, a+b): walk(c, p)
    else:
        blob = d[2*a:2*a+b]; p.write_bytes(blob)
        listing.append((str(p.relative_to(out)), 2*a, b, blob[:4].hex() + (" flag40" if no & 0x40000000 else "")))
walk(1, out)
print(f"size={2*size_w:#x} (image {len(d):#x}) entries={n}")
for l in listing: print("%-50s off=%#08x size=%7d head=%s" % l)
