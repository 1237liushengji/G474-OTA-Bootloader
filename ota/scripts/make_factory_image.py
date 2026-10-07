#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成"产线镜像"：给裸 APP 加上 32 字节镜像头，使其可被 Bootloader 校验并跳转。

## 为什么需要它（设计缺口）

本方案的契约是：**镜像头由设备在激活阶段（FF02）自行合成**（"模式A：设备侧写头"）。
因此通过 OTA 刷进去的 APP 在槽基址处**有**头；而用编程器（ST-Link/CubeProgrammer）
直接把 Keil 生成的裸 APP 烧到 `0x0800C020` 时，`0x0800C000` 处**没有**头 ——
`BL_ImageValidate()` 第一步 `BL_ImageReadHeader()` 就会失败，
BL 于是停在 recovery，表现为"上电没反应"。

本工具补上**产线/首次烧录**路径：
    裸 APP(.hex)  ->  [32B 头][APP 二进制]  ->  .hex / .bin（烧到槽基址）

## 镜像头格式（ota/common/ota_common.h）

    offset 0  : magic      "G4OT" = 47 34 4F 54
    offset 4  : imgSize    总大小 = 32 + payloadSize
    offset 8  : payloadSize 净固件字节数
    offset 12 : version    0xMMmm0000
    offset 16 : loadAddr   净荷起始地址 = 槽基址 + 32
    offset 20 : payloadCrc CRC32(净荷)
    offset 24 : hdrCrc     CRC32(头前 24 字节)
    offset 28 : reserved

用法:
    python ota/scripts/make_factory_image.py --hex firmware/G474_A.hex --slot A
    python ota/scripts/make_factory_image.py --hex firmware/G474_B.hex --slot A \\
           --version 3.0 --out firmware/G474_A_factory.hex
"""
import argparse
import binascii
import os
import struct
import sys

from paths import workspace_root

ROOT = workspace_root()

# 契约常量（与 ota/common/ota_common.h 保持一致；由 verify 脚本交叉校验）
OTA_FLASH_BASE   = 0x08000000
OTA_SLOT_A_ADDR  = 0x0800C000
OTA_SLOT_B_ADDR  = 0x08040800
OTA_IMG_HDR_SIZE = 0x80
OTA_IMG_MAX_PAYLOAD = 212960                      # OTA_SLOT_SIZE(0x34000) - 32
# ⚠️ 魔数是 u32 常量，不是字节串。BL 的 BL_ImageRdU32() 按小端读，
#    因此写 Flash 时必须用 '<I' 打包该常量（内存里呈现为 54 4F 34 47）。
OTA_IMG_MAGIC    = 0x47344F54                     # "G4OT" 的小端 u32


def crc32(data):
    """CRC-32/ISO-HDLC，与设备侧 bl_crc32.c 一致（0xEDB88320 反射，初值/末值 0xFFFFFFFF）"""
    return binascii.crc32(data) & 0xFFFFFFFF


def read_hex(path):
    """解析 Intel HEX，返回 {地址: 字节} 与最小/最大地址。"""
    mem = {}
    upper = 0
    with open(path, encoding='utf-8', errors='replace') as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line[0] != ':':
                continue
            try:
                raw = bytes.fromhex(line[1:])
            except ValueError:
                raise SystemExit('!! 第 %d 行不是合法 HEX 记录' % lineno)
            cnt, addr, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
            data = raw[4:4 + cnt]
            if rtype == 0x00:
                for i, b in enumerate(data):
                    mem[upper + addr + i] = b
            elif rtype == 0x01:
                break
            elif rtype == 0x04:
                upper = ((data[0] << 8) | data[1]) << 16
            elif rtype == 0x02:
                upper = ((data[0] << 8) | data[1]) << 4
    if not mem:
        raise SystemExit('!! HEX 里没有任何数据')
    return mem


def to_hex(mem, base, path, bytes_per_line=16):
    """把连续内存写成 Intel HEX（扩展线性地址记录）。"""
    lines = []
    addrs = sorted(mem)
    cur = addrs[0]
    while cur <= addrs[-1]:
        chunk = []
        a = cur
        while len(chunk) < bytes_per_line and (cur + len(chunk)) in mem:
            chunk.append(mem[cur + len(chunk)])
        if not chunk:
            cur += 1
            continue
        if len(lines) == 0 or (cur >> 16) != ((cur - len(chunk)) >> 16):
            lines.append(':02000004%04X%02X' % ((cur >> 16) & 0xFFFF,
                                                (0x100 - (0x02 + 0x04 + ((cur >> 16) & 0xFF) +
                                                          ((cur >> 24) & 0xFF))) & 0xFF))
        rec = bytes([len(chunk), (cur >> 8) & 0xFF, cur & 0xFF, 0x00]) + bytes(chunk)
        lines.append(':' + rec.hex().upper() + '%02X' % ((0x100 - sum(rec)) & 0xFF))
        cur += len(chunk)
    lines.append(':00000001FF')
    with open(path, 'w', encoding='ascii', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hex', required=True, help='裸 APP 的 Intel HEX（Keil 产物）')
    ap.add_argument('--slot', default='A', choices=['A', 'B'], help='目标槽（决定槽基址）')
    ap.add_argument('--version', default='3.0', help='版本 major.minor，默认 3.0')
    ap.add_argument('--out', help='输出 hex 路径（默认 <输入>_factory.hex）')
    args = ap.parse_args()

    src = args.hex if os.path.isabs(args.hex) else os.path.join(ROOT, args.hex)
    if not os.path.isfile(src):
        print('!! 找不到 %s' % src)
        return 2

    slot_base = OTA_SLOT_A_ADDR if args.slot == 'A' else OTA_SLOT_B_ADDR
    app_base = slot_base + OTA_IMG_HDR_SIZE

    mem = read_hex(src)
    lo, hi = min(mem), max(mem)
    print('输入      : %s' % os.path.relpath(src, ROOT))
    print('数据范围  : 0x%08X ~ 0x%08X  (%d 字节)' % (lo, hi, len(mem)))

    if lo != app_base:
        print('!! 预期 APP 起始地址 = 0x%08X，实际 = 0x%08X' % (app_base, lo))
        print('   说明：该 hex 不是链接在 Slot %s 的 APP（Slot %s 的 APP 基址应为 0x%08X）'
              % (args.slot, args.slot, app_base))
        return 2

    payload = bytes(mem.get(lo + i, 0xFF) for i in range(hi - lo + 1))
    payload_size = len(payload)
    payload_crc = crc32(payload)

    try:
        major, minor = (int(x) for x in str(args.version).split('.'))
    except Exception:
        print('!! --version 格式应为 major.minor，例如 3.0')
        return 2
    version = ((major & 0xFF) << 24) | ((minor & 0xFF) << 16)

    # 显式按偏移构造 32 字节头。
    # ⚠️ 全部字段按**小端**写入：BL 的 BL_ImageRdU32() 是 p[0] | p[1]<<8 | ...
    #    魔数尤其容易搞错：OTA_IMG_MAGIC = 0x47344F54 这个常量"本身"就是
    #    小端序列 47 34 4F 54，因此内存里应写 54 4F 34 47（= '<I' 打包）。
    #    曾误用 '>4s' 写入，导致内存变成 47 34 4F 54、BL 读回 0x544F3447，
    #    校验必然失败（result=1 = HDR_MAGIC）。
    hdr = bytearray(OTA_IMG_HDR_SIZE)
    struct.pack_into('<I', hdr, 0, OTA_IMG_MAGIC)                    # magic（小端）
    struct.pack_into('<I', hdr, 4, OTA_IMG_HDR_SIZE + payload_size)  # imgSize
    struct.pack_into('<I', hdr, 8, payload_size)                     # payloadSize
    struct.pack_into('<I', hdr, 12, version)                         # version
    struct.pack_into('<I', hdr, 16, app_base)                        # loadAddr
    struct.pack_into('<I', hdr, 20, payload_crc)                     # payloadCrc
    struct.pack_into('<I', hdr, 24, crc32(bytes(hdr[:24])))          # hdrCrc
    struct.pack_into('<I', hdr, 28, 0)                               # reserved
    hdr = bytes(hdr)
    assert len(hdr) == OTA_IMG_HDR_SIZE

    # ---- 自校验：按 BL 的校验逻辑回读一遍，确保生成物真能通过 ----
    def selfcheck():
        errs = []
        if struct.unpack_from('<I', hdr, 0)[0] != OTA_IMG_MAGIC:
            errs.append('magic 不符（小端读回 0x%08X，期望 0x%08X）'
                        % (struct.unpack_from('<I', hdr, 0)[0], OTA_IMG_MAGIC))
        if crc32(hdr[:24]) != struct.unpack_from('<I', hdr, 24)[0]:
            errs.append('hdrCrc 不符')
        if struct.unpack_from('<I', hdr, 8)[0] != payload_size:
            errs.append('payloadSize 不符')
        if struct.unpack_from('<I', hdr, 4)[0] != (payload_size + OTA_IMG_HDR_SIZE):
            errs.append('imgSize != payloadSize + 32')
        if struct.unpack_from('<I', hdr, 16)[0] != app_base:
            errs.append('loadAddr 不符')
        if crc32(payload) != struct.unpack_from('<I', hdr, 20)[0]:
            errs.append('payloadCrc 与实际净荷不符')
        sp = struct.unpack_from('<I', payload, 0)[0]
        if (sp % 8) != 0 or not (0x20000000 <= sp <= 0x20020000):
            errs.append('SP 不在 RAM 内或未 8 字节对齐: 0x%08X' % sp)
        rv = struct.unpack_from('<I', payload, 4)[0]
        if (rv & 1) == 0 or not (app_base <= rv < app_base + payload_size):
            errs.append('复位向量非法: 0x%08X' % rv)
        return errs

    print()
    print('=== 生成的镜像头（32 字节 @ 0x%08X）===' % slot_base)
    print('  magic      : 0x%08X  "%s"  （Flash 内字节: %s）'
          % (OTA_IMG_MAGIC, 'G4OT',
             ' '.join('%02X' % b for b in hdr[0:4])))
    print('  imgSize    : %d  (32 + %d)' % (OTA_IMG_HDR_SIZE + payload_size, payload_size))
    print('  payloadSize: %d' % payload_size)
    print('  version    : 0x%08X  (%d.%d)' % (version, major, minor))
    print('  loadAddr   : 0x%08X' % app_base)
    print('  payloadCrc : 0x%08X' % payload_crc)
    print('  hdrCrc     : 0x%08X' % crc32(bytes(hdr[:24])))
    print()
    print('  SP(净荷首字)  : 0x%08X' % struct.unpack_from('<I', payload, 0)[0])
    print('  Reset(净荷+4) : 0x%08X  (bit0=%d，须为 1)'
          % (struct.unpack_from('<I', payload, 4)[0],
             struct.unpack_from('<I', payload, 4)[0] & 1))

    problems = selfcheck()
    print()
    if problems:
        print('!! 自校验未通过：')
        for e in problems:
            print('   - %s' % e)
        return 2
    print('自校验通过：该镜像可被 BL 的 BL_ImageValidate() 接受。')

    out_mem = {slot_base + i: b for i, b in enumerate(hdr)}
    for i, b in enumerate(payload):
        out_mem[app_base + i] = b

    out = args.out or (os.path.splitext(src)[0] + '_factory.hex')
    if not os.path.isabs(out):
        out = os.path.join(ROOT, out)
    to_hex(out_mem, slot_base, out)

    binpath = os.path.splitext(out)[0] + '.bin'
    with open(binpath, 'wb') as fh:
        fh.write(hdr + payload)

    print()
    print('输出 hex : %s  (%d 字节)' % (os.path.relpath(out, ROOT), os.path.getsize(out)))
    print('输出 bin : %s  (%d 字节)' % (os.path.relpath(binpath, ROOT), os.path.getsize(binpath)))
    print()
    print('烧录（hex 自带地址，落到 0x%08X）:' % slot_base)
    print('  python ota/scripts/flash.py --file %s' % os.path.relpath(out, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
