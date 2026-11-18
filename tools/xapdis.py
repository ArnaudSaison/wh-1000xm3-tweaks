"""Minimal XAP (CSR BlueCore VM) disassembler, port of radare2 libr/arch/p/xap/dis.c (sorbo '07).
Words are big-endian in the converted image. Bitfields: mode[1:0] reg[3:2] opcode[7:4] operand[15:8].
usage: xapdis.py vm.app [start_word end_word]   |  import: disasm(words) -> [(addr, word, text, imm)]"""
import sys, struct
REG = ["AH", "AL", "X", "Y"]
OPS = {1: "ld", 3: "add", 4: "addc", 5: "sub", 6: "subc", 7: "nadd", 8: "cmp", 0xb: "or", 0xc: "and", 0xd: "xor"}
FIXED = {0x0000: "nop", 0x0004: "brk", 0x0008: "sleep", 0x000c: "sif", 0xff09: "bc", 0xfe09: "brxl", 0x00e2: "rts", 0x0009: "u"}
def sx8(v): return v - 0x100 if v & 0x80 else v
def disasm(words, base=0):
    out = []; pre = 0; npre = 0
    for i, w in enumerate(words):
        mode, reg, opc, opd = w & 3, (w >> 2) & 3, (w >> 4) & 15, w >> 8
        if w in FIXED and not (w == 0 and npre):
            out.append((base+i, w, FIXED[w], None)); pre = npre = 0; continue
        if opc == 0 and reg == 0 and mode == 0:  # prefix
            pre = (pre << 8 | opd) if npre else opd; npre += 1; out.append((base+i, w, f"prefix {opd:#04x}", None)); continue
        val = sx8(opd) + ((sx8(pre & 0xff) << 8) if npre else 0) + ((sx8((pre >> 8) & 0xff) << 16) if npre == 2 else 0)
        imm = None
        if opc in OPS or opc == 2:
            name = OPS.get(opc, "print" if mode == 0 else "st")
            if mode == 0: txt = f"{name} {REG[reg]}, #{val:#x}"; imm = val
            elif mode == 1: txt = f"{name} {REG[reg]}, @{val & 0xffffff:#x}"
            else: txt = f"{name} {REG[reg]}, @({val:#x}, {'X' if mode == 2 else 'Y'})"
        elif opc == 9:
            name = ["smult", "sdiv", "tst", "bsr"][reg]
            txt = f"{name} " + (f"#{val:#x}" if mode == 0 and reg != 3 else f"{'rel' if mode == 0 else '@'} {val:#x}"); imm = val if mode == 0 and reg != 3 else None
        elif opc == 0xa: txt = f"{['asl','asr','rol','ror'][reg]} #{val:#x}"
        elif opc in (0xe, 0xf):
            name = (["bra", "blt", "bpl", "bmi"] if opc == 0xe else ["bne", "beq", "bcc", "bcs"])[reg]
            txt = f"{name} " + (f"{base+i+val:#x}" if mode == 0 else f"@{val:#x}" if mode == 1 else f"({val:#x}, X)")
        elif opc == 0: txt = f"sys {w:#06x}"
        else: txt = f"dc {w:#06x}"
        out.append((base+i, w, txt, imm)); pre = npre = 0
    return out
if __name__ == "__main__":
    d = open(sys.argv[1], "rb").read(); words = struct.unpack(f">{len(d)//2}H", d[:len(d)//2*2])
    a = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0; b = int(sys.argv[3], 0) if len(sys.argv) > 3 else len(words)
    for addr, w, t, _ in disasm(words[a:b], a): print(f"{addr:06x}: {w:04x}  {t}")
