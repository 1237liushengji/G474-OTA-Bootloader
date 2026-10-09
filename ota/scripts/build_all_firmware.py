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
    # 【2026-10-09 修复】原实现只切CAN 角色，**不切链接基址**，导致
    # G474_B.hex 与 G474_A.hex 链接地址完全相同（都是 0x0800C080）——
    # 名为"B 板固件"实际仍跑在 Slot A。make_factory_image.py --slot B
    # 会正确拒绝它（"预期 0x08040880，实际 0x0800C080"）。
    # 现在两个角色各自绑定自己的槽位：角色 A -> Slot A，角色 B -> Slot B。
    orig_can = open(CAN_H, encoding='utf-8', errors='replace').read()
    try:
        for role, slot, outname in (('CAN_NODE_A', 'A', 'G474_A.hex'),
                                    ('CAN_NODE_B', 'B', 'G474_B.hex')):
            print('[3] APP  角色 = %s  槽位 = Slot %s' % (role, slot))

            # 3a) 先切链接基址（IROM + OCR_RVCT4 + VECT_TAB_OFFSET 三处必须一致）
            r = subprocess.run([sys.executable, os.path.join(ROOT, 'ota', 'scripts', 'fix_app_irom.py'),
                                '--slot', slot, '--apply'],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print('    !! 切换到 Slot %s 失败（fix_app_irom.py 退出码 %d）' % (slot, r.returncode))
                print('    ' + (r.stdout or r.stderr).strip().replace('\n', '\n    '))
                continue

            # 3b) 再切 CAN 角色
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
        # 链接基址也必须还原为 Slot A，否则下次打开工程时基址是 B 的
        subprocess.run([sys.executable, os.path.join(ROOT, 'ota', 'scripts', 'fix_app_irom.py'),
                        '--slot', 'A', '--apply'], capture_output=True, text=True)
        # 必须**重编一次**：工程配置已还原成Slot A，但 G474.map 仍停留在最后一次
        # 编译（Slot B）的结果。不重编的话，verify_layout.py --slot A 会报
        # "向量表 0x08040880 期望 0x0800C080"，而实际上 Slot A 固件早已正确产出。
        e, w, s, _ = build(APP_PROJ, os.path.join(os.environ.get('TEMP', '.'), 'fw_app.log'))
        print()
        print('  can.h 与 APP 链接基址已还原为 Slot A（并重编以同步 %s）' % os.path.basename(APP_HEX))
        print('  重编: %d Error, %d Warning   %s' % (e, w, s))

    print()
    print('到位固件清单（%s/）：' % os.path.relpath(OUT, ROOT))
    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        print('  %-26s %8d 字节  sha256:%s' % (f, os.path.getsize(p), sha16(p)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
