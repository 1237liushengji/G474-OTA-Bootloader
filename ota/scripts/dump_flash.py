#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从目标芯片读回 Flash 内容并解析 OTA 镜像头 —— 用硬证据替代推测。

背景：BL 报 `result=1 = BL_IMG_ERR_HDR_MAGIC`，即槽基址(0x0800C000)找不到 G4OT 魔数。
本工具直接把芯片内容 dump 出来，回答：
  * 槽基址到底有什么（是否为空 0xFF / 是否有魔数）
  * BL 区是否有有效向量表
  * 控制区是否有配置

用法:
    python ota/scripts/dump_flash.py --slot A         读槽 A 头部
    python ota/scripts/dump_flash.py --addr 0x0800C000 --len 64
    python ota/scripts/dump_flash.py --all            读 BL/槽A/槽B/控制区 的关键区域
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
import binascii

from paths import workspace_root

ROOT = workspace_root()
CLI = r'D:\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe'

OTA_SLOT_A_ADDR  = 0x0800C000
OTA_SLOT_B_ADDR  = 0x08040800
OTA_FLASH_BASE   = 0x08000000
OTA_IMG_HDR_SIZE = 0x80
OTA_IMG_MAGIC    = 0x47344F54          # "G4OT"


def read_flash(addr, length):
    """用 CubeProgrammer 读回一段 Flash，返回 bytes。"""
    n = (length + 3) // 4
    fd, path = tempfile.mkstemp(suffix='.bin')
    os.close(fd)
    try:
        cmd = [CLI, '-c', 'port=SWD', 'mode=UR',
               '-u', hex(addr), hex(n * 4), path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = (r.stdout or '') + (r.stderr or '')
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return None, out
        data = open(path, 'rb').read()
        return data[:length], out
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def hexdump(data, base, width=16):
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = ' '.join('%02X' % b for b in chunk)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append('  %08X  %-*s  |%s|' % (base + off, width * 3 - 1, hx, asc))
    return lines


def parse_header(data, label, addr):
    """按 BL 的字段布局解析镜像头。

    ⚠️ 全部字段按**小端**解析：BL 的 BL_ImageRdU32() 是 p[0] | p[1]<<8 | ...
       曾误用大端（'>I'）解析，把字节反转的魔数误报为"正确"，
       造成假阳性、误导排查数轮。
    """
    print('--- %s @ 0x%08X ---' % (label, addr))
    if len(data) < OTA_IMG_HDR_SIZE:
        print('  (读回数据不足 %d 字节：需要整个槽首保留区才能判定)' % OTA_IMG_HDR_SIZE)
        return
    magic = struct.unpack_from('<I', data, 0)[0]
    print('  magic      : 0x%08X  %s' % (magic,
          '"G4OT" ← 正确（小端）' if magic == OTA_IMG_MAGIC
          else '!! 不是 G4OT（期望 0x%08X）' % OTA_IMG_MAGIC))
    if all(b == 0xFF for b in data[:OTA_IMG_HDR_SIZE]):
        print('  >>> 该区域**全为 0xFF（已擦除）** —— 镜像没烧进去，或被后续操作擦掉')
        return
    if all(b == 0x00 for b in data[:OTA_IMG_HDR_SIZE]):
        print('  >>> 该区域全为 0x00')
        return
    imgSize  = struct.unpack_from('<I', data, 4)[0]
    paySize  = struct.unpack_from('<I', data, 8)[0]
    ver      = struct.unpack_from('<I', data, 12)[0]
    loadAddr = struct.unpack_from('<I', data, 16)[0]
    payCrc   = struct.unpack_from('<I', data, 20)[0]
    hdrCrc   = struct.unpack_from('<I', data, 24)[0]
    print('  imgSize    : %u' % imgSize)
    print('  payloadSize: %u' % paySize)
    print('  version    : 0x%08X' % ver)
    print('  loadAddr   : 0x%08X' % loadAddr)
    print('  payloadCrc : 0x%08X' % payCrc)
    calc = binascii.crc32(data[:24]) & 0xFFFFFFFF
    print('  hdrCrc     : 0x%08X   (实算 0x%08X) %s'
          % (hdrCrc, calc, 'OK' if hdrCrc == calc else '!! 不符'))

    # 逐条按 BL_ImageReadHeader 的判据复核，直接给出"BL 会不会接受"
    verdict = []
    if magic != OTA_IMG_MAGIC:
        verdict.append('HDR_MAGIC 失败')
    if hdrCrc != calc:
        verdict.append('HDR_CRC 失败')
    if paySize == 0 or paySize > 212960:
        verdict.append('SIZE_RANGE 失败')
    if imgSize != paySize + OTA_IMG_HDR_SIZE:
        verdict.append('SIZE_RANGE 失败(imgSize≠payloadSize+32)')
    if loadAddr != addr + OTA_IMG_HDR_SIZE:
        verdict.append('LOAD_ADDR 失败(应为 0x%08X)' % (addr + OTA_IMG_HDR_SIZE))
    print('  BL 判据    : %s' % ('全部满足 → BL 会接受该镜像'
                                if not verdict else '!! ' + '; '.join(verdict)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--addr', help='起始地址，如 0x0800C000')
    ap.add_argument('--len', type=int, default=64, help='读取长度（默认 64）')
    ap.add_argument('--slot', choices=['A', 'B'], help='读槽 A/B 的镜像头')
    ap.add_argument('--all', action='store_true', help='读全部关键区域')
    args = ap.parse_args()

    if not os.path.isfile(CLI):
        print('!! 未找到 %s' % CLI)
        return 2

    if args.slot:
        addr = OTA_SLOT_A_ADDR if args.slot == 'A' else OTA_SLOT_B_ADDR
        data, out = read_flash(addr, 64)
        if data is None:
            print(out)
            return 1
        print(hexdump(data[:32], addr))
        print()
        parse_header(data, 'Slot %s 镜像头' % args.slot, addr)
        return 0

    if args.addr:
        addr = int(args.addr, 0)
        data, out = read_flash(addr, args.len)
        if data is None:
            print(out)
            return 1
        print('\n'.join(hexdump(data, addr)))
        return 0

    if args.all:
        regions = [
            ('BL 向量表', OTA_FLASH_BASE, 32),
            ('控制区（配置主份）', OTA_FLASH_BASE + 0x1D8, 48),
            ('控制区（第二副本）', OTA_FLASH_BASE + 0x400, 48),
            ('Slot A 镜像头', OTA_SLOT_A_ADDR, OTA_IMG_HDR_SIZE),
            ('Slot B 镜像头', OTA_SLOT_B_ADDR, OTA_IMG_HDR_SIZE),
            ('控制区镜像份', OTA_FLASH_BASE + 0x40000 + 0x1D8, 48),
        ]
        for label, addr, ln in regions:
            print('=' * 70)
            data, out = read_flash(addr, ln)
            if data is None:
                print('%s @ 0x%08X 读取失败' % (label, addr))
                print(out[:400])
                continue
            print('%s @ 0x%08X' % (label, addr))
            print('\n'.join(hexdump(data, addr)))
            if 'Slot' in label:
                print()
                parse_header(data, label, addr)
            print()
        return 0

    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
