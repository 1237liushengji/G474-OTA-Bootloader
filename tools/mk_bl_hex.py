#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 fromelf 导出的 ER_IROM1/ER_IROM2 合成单个 Intel HEX。
布局来自 bl.sct：ER_IROM1 @0x08000000（页0，只有向量表）、ER_IROM2 @0x08000800（BL 代码）。"""
import os, sys

def ihex_record(rtype, addr, data):
    b = [len(data), (addr >> 8) & 0xFF, addr & 0xFF, rtype] + list(data)
    b.append((-sum(b)) & 0xFF)
    return ":" + "".join("%02X" % x for x in b)

def main():
    src = sys.argv[1]
    out = sys.argv[2]
    r1 = open(os.path.join(src, "ER_IROM1"), "rb").read()
    r2 = open(os.path.join(src, "ER_IROM2"), "rb").read()
    lines = [ihex_record(0x04, 0x08000000, b"\x08\x00\x00\x00")]   # 扩展线性地址
    for base, blob in ((0x08000000, r1), (0x08000800, r2)):
        for i in range(0, len(blob), 32):                # ★ 单记录<=32 字节（255 上限）
            lines.append(ihex_record(0x00, (base + i) & 0xFFFF, blob[i:i + 32]))
    lines.append(ihex_record(0x05, 0x08000000, b"\x08\x00"))          # 入口 = 复位向量
    lines.append(":00000001FF")
    open(out, "w").write("\n".join(lines) + "\n")
    print("[OK ] %s  IROM1=%d @0x08000000  IROM2=%d @0x08000800" % (out, len(r1), len(r2)))

if __name__ == "__main__":
    main()
