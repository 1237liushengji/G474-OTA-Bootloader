#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通过 SWD 读取板上 Flash 的一个区间并计算 CRC32/ISO-HDLC。
用法: python tools/fw_crc_swd.py <sn> <hex_addr> <size_hex>
注意：每次 -r32 都会 halt 内核，读大区间很慢；先读头再按需读 payload。"""
import re, subprocess, sys, zlib

CLI = "D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"

def read(sn, addr, size):
    args = [CLI, "-c", "port=SWD", "sn=%s" % sn, "mode=hotplug", "-halt"]
    CH = 0x400
    a = addr
    left = size
    while left > 0:
        n = min(CH, left)
        args.append("-r32")
        args.append("0x%08X" % a)
        args.append(str(n))
        a += n
        left -= n
    out = subprocess.run(args, capture_output=True)
    txt = (out.stdout or b"").decode("utf-8", errors="replace")
    data = bytearray()
    for m in re.finditer(r"^0x([0-9A-Fa-f]{8})\s*:\s*((?:[0-9A-Fa-f]{8}\s*)+)", txt, re.M):
        for w in m.group(2).split():
            data += bytes.fromhex(w)[::-1]   # 小端
    return bytes(data)

if __name__ == "__main__":
    sn = sys.argv[1]
    hdr = read(sn, int(sys.argv[2], 16), 32)
    print("hdr:", hdr.hex())
    img_size  = int.from_bytes(hdr[4:8], "little")
    pay_size  = int.from_bytes(hdr[8:12], "little")
    load_addr = int.from_bytes(hdr[16:20], "little")
    pay_crc   = int.from_bytes(hdr[20:24], "little")
    hdr_crc   = int.from_bytes(hdr[24:28], "little")
    calc_hdr  = zlib.crc32(hdr[:24]) & 0xFFFFFFFF
    print("imgSize=0x%X paySize=0x%X load=0x%08X payCrc=0x%08X" % (img_size, pay_size, load_addr, pay_crc))
    print("hdrCrc stored=0x%08X calc=0x%08X %s" % (hdr_crc, calc_hdr, "OK" if hdr_crc == calc_hdr else "MISMATCH"))
    body = read(sn, load_addr, pay_size)
    calc = zlib.crc32(body) & 0xFFFFFFFF
    print("payload read %d bytes, crc calc=0x%08X  %s" % (len(body), calc, "OK" if calc == pay_crc else "*** MISMATCH ***"))
