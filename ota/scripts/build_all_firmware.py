#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键产出到位固件，并做自检：

  BL_bare_blink_TEST.hex  裸机闪灯自检（3KB，用于判定"MCU 到底有没有在跑"）
  BL.hex                  Bootloader（带 7 步 LED 分步自检）
  G474_A.hex              APP，CAN_NODE_ROLE = CAN_NODE_A（发给板A）
  G474_B.hex              APP，CAN_NODE_ROLE = CAN_NODE_B（发给板B）

每产出一个都打印大小与 SHA256 前 16 位，便于对照烧录时没拿错文件。

用法: python ota/scripts/build_all_firmware.py [--skip-app]
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys

from paths import workspace_root

ROOT = workspace_root()
UV = r'D:\U5\UV4\UV4.exe'
BL_PROJ = os.path.join(ROOT, 'ota', 'bootloader', 'MDK-ARM', 'BL.uvprojx')
APP_PROJ = os.path.join(ROOT, 'G474', 'MDK-ARM', 'G474.uvprojx')
CAN_H = os.path.join(ROOT, 'G474', 'Drivers', 'User', 'Inc', 'can.h')
BL_MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')
BL_HEX = os.path.join(ROOT, 'ota', 'bootloader', 'MDK-ARM', 'G474', 'BL.hex')
APP_HEX = os.path.join(ROOT, 'G474', 'MDK-ARM', 'G474', 'G474.hex')
OUT = os.path.join(ROOT, 'firmware')


def build(proj, log):
    with open(log, 'w') as fh:
        p = subprocess.run([UV, '-r', proj, '-j0', '-o', log],
                           capture_output=True, text=True)
    txt = open(log, encoding='utf-8', errors='replace').read()
    errs = re.search(r'(\d+) Error\(s\), (\d+) Warning\(s\)', txt)
    size = re.search(r'Program Size:\s*(.+)', txt)
    return (int(errs.group(1)) if errs else -1,
            int(errs.group(2)) if errs else -1,
            size.group(1).strip() if size else '?', txt)


def sha16(path):
    h = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    return h[:16]


def emit(src, dst):
    if not os.path.exists(src):
        print('    !! 源文件不存在: %s' % src)
        return False
    shutil.copy2(src, dst)
    print('    %-26s %8d 字节  sha256:%s' % (os.path.basename(dst),
                                            os.path.getsize(dst), sha16(dst)))
    return True


def main():
    os.makedirs(OUT, exist_ok=True)
    skip_app = '--skip-app' in sys.argv
    print('输出目录: %s' % os.path.relpath(OUT, ROOT))
    print()

    # ---- 1) 裸机闪灯 ----
    print('[1] 裸机闪灯自检固件')
    t = open(BL_MAIN, encoding='utf-8', errors='replace').read()
    orig_main = t
    t2 = re.sub(r'#define BL_BARE_BLINK\s+0', '#define BL_BARE_BLINK 1', t)
    open(BL_MAIN, 'w', encoding='utf-8', newline='').write(t2)
    e, w, s, _ = build(BL_PROJ, os.path.join(os.environ.get('TEMP', '.'), 'fw_bare.log'))
    print('    编译: %d Error, %d Warning   %s' % (e, w, s))
    if e == 0:
        emit(BL_HEX, os.path.join(OUT, 'BL_bare_blink_TEST.hex'))
    open(BL_MAIN, 'w', encoding='utf-8', newline='').write(orig_main)   # 还原

    # ---- 2) 正常 BL ----
    print('[2] Bootloader（含 7 步 LED 自检）')
    e, w, s, _ = build(BL_PROJ, os.path.join(os.environ.get('TEMP', '.'), 'fw_bl.log'))
    print('    编译: %d Error, %d Warning   %s' % (e, w, s))
    if e == 0:
        emit(BL_HEX, os.path.join(OUT, 'BL.hex'))

    if skip_app:
        print()
        print('（--skip-app：跳过 APP 的两个角色版本）')
        return 0

    # ---- 3) APP 两个角色 ----
    orig_can = open(CAN_H, encoding='utf-8', errors='replace').read()
    try:
        for role, outname in (('CAN_NODE_A', 'G474_A.hex'),
                              ('CAN_NODE_B', 'G474_B.hex')):
            print('[3] APP  角色 = %s' % role)
            cur = open(CAN_H, encoding='utf-8', errors='replace').read()
            new, nsub = re.subn(r'(#define CAN_NODE_ROLE\s+)CAN_NODE_[AB]',
                                r'\g<1>' + role, cur, count=1)
            if nsub != 1:
                print('    !! can.h 里未找到 CAN_NODE_ROLE 宏（匹配 %d 次）' % nsub)
                continue
            open(CAN_H, 'w', encoding='utf-8', newline='').write(new)
            e, w, s, _ = build(APP_PROJ, os.path.join(os.environ.get('TEMP', '.'), 'fw_app.log'))
            print('    编译: %d Error, %d Warning   %s' % (e, w, s))
            if e == 0:
                emit(APP_HEX, os.path.join(OUT, outname))
    finally:
        open(CAN_H, 'w', encoding='utf-8', newline='').write(orig_can)
        print()
        print('  can.h 已还原为原始角色设置')

    print()
    print('到位固件清单（%s/）：' % os.path.relpath(OUT, ROOT))
    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        print('  %-26s %8d 字节  sha256:%s' % (f, os.path.getsize(p), sha16(p)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
