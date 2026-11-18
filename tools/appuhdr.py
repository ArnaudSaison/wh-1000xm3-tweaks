"""Walk a Qualcomm/CSR ADK upgrade file (APPUHDR2 / PARTDATA / APPUPFTR chunks)."""
import sys, struct, pathlib
f = pathlib.Path(sys.argv[1]); d = f.read_bytes(); out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
o = 0
while o < len(d):
    tag = d[o:o+8]; ln, = struct.unpack(">I", d[o+8:o+12]); body = d[o+12:o+12+ln]
    print(f"{o:#09x} {tag!r} len={ln:#x}", end=" ")
    if tag == b"APPUHDR2":
        var = body[:8]; maj, mnr, n = struct.unpack(">HHH", body[8:14])
        print(f"variant={var!r} upgrade_ver={maj}.{mnr} compat={[struct.unpack('>HH', body[14+4*i:18+4*i]) for i in range(n)]}", end="")
    elif tag == b"PARTDATA":
        part, = struct.unpack(">H", body[:2]); print(f"partition={part} first={body[2:14].hex()} magic={body[2:10]!r}", end="")
        if out: (out / f"part{part:02d}_at{o:07x}.bin").write_bytes(body[2:])
    else:
        print(f"body[:32]={body[:32].hex()}", end="")
        if out: (out / f"{tag.decode(errors='replace')}_at{o:07x}.bin").write_bytes(body)
    print(); o += 12 + ln
print("end", hex(o), "filesize", hex(len(d)))
