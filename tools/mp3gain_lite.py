"""Lossless MP3 Layer III attenuation: subtract N from every granule's global_gain (1 step = 1.5 dB).
Same principle as mp3gain; frame sizes are unchanged, so the file size is identical.
usage: mp3gain_lite.py in.mp3 out.mp3 steps"""
import sys
BITRATES = {1: [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320], 2: [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160]}
RATES = {3: [44100,48000,32000], 2: [22050,24000,16000], 0: [11025,12000,8000]}
def frames(d):
    o = 0
    if d[:3] == b"ID3":
        o = 10 + ((d[6]&0x7f)<<21 | (d[7]&0x7f)<<14 | (d[8]&0x7f)<<7 | (d[9]&0x7f))
    while o + 4 <= len(d):
        h = int.from_bytes(d[o:o+4], "big")
        if h >> 21 != 0x7ff or (h >> 17) & 3 != 1:  # sync + layer III
            o += 1; continue
        ver = (h >> 19) & 3; br_i = (h >> 12) & 15; sr_i = (h >> 10) & 3
        if ver == 1 or br_i in (0, 15) or sr_i == 3: o += 1; continue
        lsf = ver != 3
        br = BITRATES[2 if lsf else 1][br_i] * 1000; sr = RATES[ver][sr_i]; pad = (h >> 9) & 1
        size = (72 if lsf else 144) * br // sr + pad
        yield o, h, lsf, size
        o += size
def patch(d, steps):
    d = bytearray(d); n = 0; clipped = 0
    for o, h, lsf, size in frames(d):
        crc = not ((h >> 16) & 1); mono = ((h >> 6) & 3) == 3; nch = 1 if mono else 2
        bitpos = (o + 4 + (2 if crc else 0)) * 8
        bitpos += (8 if lsf else 9) + ((1 if mono else 2) if lsf else (5 if mono else 3))
        if not lsf: bitpos += 4 * nch  # scfsi
        for gr in range(1 if lsf else 2):
            for ch in range(nch):
                gpos = bitpos + 12 + 9
                v = 0
                for i in range(8): v = v << 1 | (d[(gpos+i)//8] >> (7 - (gpos+i) % 8)) & 1
                nv = max(0, v - steps); clipped += nv != v - steps
                for i in range(8):
                    b = (nv >> (7 - i)) & 1; p = gpos + i
                    d[p//8] = (d[p//8] & ~(1 << (7 - p % 8))) | (b << (7 - p % 8))
                n += 1
                gr_bits = 12+9+8+(9 if lsf else 4)+1
                ws = (d[(gpos+8+(9 if lsf else 4))//8] >> (7 - (gpos+8+(9 if lsf else 4)) % 8)) & 1
                gr_bits += (2+1+5+5+3+3+3) if ws else (15+4+3)
                gr_bits += (2 if lsf else 3)  # (preflag) scalefac_scale, count1table_select
                bitpos += gr_bits
    return bytes(d), n, clipped
if __name__ == "__main__":
    src = open(sys.argv[1], "rb").read(); out, n, c = patch(src, int(sys.argv[3]))
    open(sys.argv[2], "wb").write(out); print(f"{sys.argv[1].split('/')[-1]}: {n} granules -{int(sys.argv[3])*1.5:.1f} dB clipped={c} size {len(src)}->{len(out)}")
