"""Split a CSR 'CSR-dfu2' file (BlueCore DFU, AN093; layout from ramikg/csr-dfu-parser CSRDFU.bt).
usage: csrdfu.py file.dfu outdir [skip_bytes]"""
import sys, struct, zlib, pathlib, json
d = pathlib.Path(sys.argv[1]).read_bytes()[int(sys.argv[3]) if len(sys.argv) > 3 else 0:]
out = pathlib.Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
crc = lambda b: zlib.crc32(b) ^ 0xffffffff  # CSR uses non-final-xored CRC32
assert d[:7] == b"CSR-dfu"
ver, flen, hlen = struct.unpack_from("<HIH", d, 8)
desc = d[16:80].rstrip(b" \0").decode()
ss_len, as_len, ps_len, sps_len, aps_len, desc_len = struct.unpack_from("<6I", d, 80)
o = 104
ssd = [struct.unpack_from("<QQI", d, o + 20*i) for i in range(16)]; o += 320
asd = [struct.unpack_from("<HqI", d, o + 14*i) for i in range(16)]; o += 224
psd = [struct.unpack_from("<QIII", d, o + 20*i) for i in range(16)]; o += 320
fsd = [struct.unpack_from("<IHHHHI", d, o + 16*i) for i in range(16)]; o += 256
print(f"ver={ver} file_len={flen:#x} (have {len(d):#x}) hdr_len={hlen:#x} desc={desc!r} parsed_hdr_end={o:#x} desc_len={desc_len}")
o += desc_len
pos = o
def take(n):
    global pos; b = d[pos:pos+n]; pos += n; return b
for i, (bc, fg, ln) in enumerate(ssd):
    if not ln: continue
    blob = take(ln); c, plen, clen, v = struct.unpack_from("<IIHH", blob, 8)
    ok = crc(blob[12:]) == c
    print(f"stack[{i}] bc={bc:#x} fg={fg:#x} len={ln:#x} prog={plen:#x} const={clen:#x} ver={v} crc_ok={ok}")
    (out / f"stack{i}.bin").write_bytes(blob)
for i, (vm, s, ln) in enumerate(asd):
    if ln: print(f"app[{i}] vm={vm} s={s} len={ln:#x}"); (out / f"app{i}.bin").write_bytes(take(ln))
def ps(kind, i, ln):
    blob = take(ln); clear, nkeys = struct.unpack_from("<HH", blob, 0); p = 4; recs = []
    for _ in range(nkeys):
        act, key, n = struct.unpack_from("<HHH", blob, p); p += 6
        words = list(struct.unpack_from(f"<{n}H", blob, p)); p += 2*n
        recs.append({"action": act, "key": key, "key_hex": f"{key:#06x}", "len": n, "data": " ".join(f"{w:04x}" for w in words)})
    sig = blob[p:len(blob)-4]
    ok = crc(blob[:-4]) == struct.unpack_from("<I", blob, len(blob)-4)[0]
    print(f"{kind}[{i}] len={ln:#x} clear={clear} keys={nkeys} trailing_sig_bytes={len(sig)} sig_nonzero={any(sig)} crc_ok={ok}")
    (out / f"{kind}{i}.json").write_text(json.dumps(recs, indent=1))
    (out / f"{kind}{i}.bin").write_bytes(blob)
for i, (bc, ln, sl, al) in enumerate(psd):
    if ln: ps("ps", i, ln)
for i, (bc, ln, sl, al) in enumerate(psd):
    if sl: ps("sps", i, sl)
for i, (bc, ln, sl, al) in enumerate(psd):
    if al: ps("aps", i, al)
for i, (ln, fs, vm, u1, u2, real) in enumerate(fsd):
    if not real: continue
    blob = take(real)
    print(f"fs[{i}] len={ln:#x} fs={fs:#x} vm={vm:#x} u={u1:#x},{u2:#x} real={real:#x} id={blob[:8]!r} sig_nonzero={any(blob[8:136])}")
    (out / f"fs{i}.bin").write_bytes(blob)
    # 'File' image is PDP-endian (LE u16 words); convert to big-endian words so csrfs.py can read it
    img = blob[140:]; be = b"".join(img[j:j+2][::-1] for j in range(0, len(img) - len(img) % 2, 2))
    (out / f"fs{i}_be.bin").write_bytes(be)
fc, = struct.unpack_from("<I", d, pos); print(f"file_crc_ok={crc(d[:pos]) == fc} at {pos:#x}; suffix={d[pos+4:pos+20].hex()}")
