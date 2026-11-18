"""Locate and print the button-timing table of the WH-1000XM3 vm.app.
The table is a list of (data_ptr, count) descriptors pointing at count * (duration_ms, kind, event_id) entries.
It is found by searching for descriptor runs whose pointer deltas equal 3 * count (layout seen in fw 4.5.2).
Data address -> file word: word = ptr + delta, delta solved from the first descriptor.
usage: button_table.py vm.app"""
import sys, struct
d = open(sys.argv[1], "rb").read(); w = struct.unpack(f">{len(d)//2}H", d)
best = None
for i in range(len(w) - 8):
    run = []; j = i
    while j + 3 < len(w):
        p, n = w[j], w[j+1]
        if p == 0 and n == 0: run.append(None); j += 2; continue
        if not (1 <= n <= 16 and p >= 0x8000): break
        nxt = j + 2
        while nxt + 1 < len(w) and w[nxt] == 0 and w[nxt+1] == 0: nxt += 2
        if w[nxt] != p + 3 * n and len([r for r in run if r]) < 2: break
        run.append((p, n)); j += 2
        if w[nxt] != p + 3 * n: break
    real = [r for r in run if r]
    if len(real) >= 4 and (best is None or len(real) > len(best[1])): best = (i, real)
assert best, "table not found"
i, descs = best
# the entries immediately precede the descriptor list: last group ends right before it
last_p, last_n = descs[-1]
delta = i - (last_p + 3 * last_n)
print(f"descriptor list at file word {i:#x}, {len(descs)} groups, data->file delta {delta:#x}")
for g, (p, n) in enumerate(descs, 1):
    print(f"group {g} @data {p:#06x} (file word {p+delta:#x}, byte {2*(p+delta):#x} in vm.app):")
    for e in range(n):
        t, kind, ev = w[p + delta + 3*e: p + delta + 3*e + 3]
        if t & 0x8000:
            desc = f"special {t:#06x} param={kind} ms (double press window?)"
        else:
            desc = f"{t:6d} ms  kind={kind} ({'on hold reached' if kind == 0 else 'on release after hold'}?)"
        print(f"   [{e}] {desc:58s} -> event {ev:#04x}   raw={t:04x} {kind:04x} {ev:04x}")
