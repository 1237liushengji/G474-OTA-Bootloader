import sys

path = sys.argv[1]
base = 0
lo = None
hi = None
addrs = []

with open(path, 'r') as f:
    for line in f:
        line = line.strip()
        if not line.startswith(':'):
            continue
        n = int(line[1:3], 16)
        off = int(line[3:7], 16)
        typ = int(line[7:9], 16)
        if typ == 0x04:
            base = int(line[9:13], 16) << 16
        elif typ == 0x00:
            a = base + off
            addrs.append((a, n))
            lo = a if lo is None else min(lo, a)
            hi = a + n if hi is None else max(hi, a + n)
        elif typ == 0x01:
            break

print('file    :', path)
print('range   : 0x%08X .. 0x%08X  (%d bytes)' % (lo, hi, hi - lo))
print('records :', len(addrs))

# 检查是否触及 BL 页（0x08000000..0x080007FF）
bl_hits = [a for a, n in addrs if a < 0x08000800]
print('touches BL page (<0x08000800):', len(bl_hits), bl_hits[:5])
