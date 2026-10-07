#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正 APP 工程的链接地址（幂等）。

问题背景：Keil 在构建时会依据 **OCR_RVCT4**（Linker 页的内存布局）自动生成
分散加载文件 `MDK-ARM/G474/G474.sct`，并**覆盖** `<IROM>` 块里的设置。
因此只改 `<IROM>` 不够 —— 必须同时改 OCR_RVCT4 与 IROM，二者一致才会生效。

本脚本把两者都设为 Slot A：起始 0x0800C080、大小 0x33F80（= 0x40000 - 0xC080）。

用法:
    python ota/scripts/fix_app_irom.py            # 检查并报告
    python ota/scripts/fix_app_irom.py --apply    # 修正
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
PROJ = os.path.join(ROOT, 'G474', 'MDK-ARM', 'G474.uvprojx')

SLOT_A_APP = 0x0800C080
SLOT_A_END = 0x08040000                     # Bank1 上界
SIZE = SLOT_A_END - SLOT_A_APP              # 0x33F80
NEW_START = '0x800C080'
NEW_SIZE = '0x33F80'


def patch_block(text, tag, start, size):
    """把 <tag>...<StartAddress>/<Size> 改成给定值（容忍块内注释；忽略大小写）。"""
    pat = (r'(<' + tag + r'>\s*<Type>1</Type>\s*)'
           r'(?:<!--.*?-->\s*)?'
           r'<StartAddress>[^<]*</StartAddress>\s*<Size>[^<]*</Size>')
    new = r'\g<1><StartAddress>' + start + r'</StartAddress>\n                <Size>' + size + r'</Size>'
    return re.subn(pat, new, text, flags=re.S | re.I)


def read_block(text, tag):
    """读出 <tag> 块的起始/大小（大小写不敏感）。"""
    m = re.search(r'<' + tag + r'>\s*<Type>1</Type>\s*(?:<!--.*?-->\s*)?'
                  r'<StartAddress>([^<]+)</StartAddress>\s*<Size>([^<]+)</Size>',
                  text, re.S | re.I)
    return (m.group(1), m.group(2)) if m else (None, None)


def main():
    apply_ = '--apply' in sys.argv
    text = open(PROJ, encoding='utf-8', errors='replace').read()
    orig = text

    results = []
    for tag in ('IROM', 'OCR_RVCT4'):
        text, n = patch_block(text, tag, NEW_START, NEW_SIZE)
        results.append((tag, n))

    # 复核
    ok = True
    print('目标: APP 链接基址 0x%08X  大小 0x%X (%d 字节)  [Slot A]' % (SLOT_A_APP, SIZE, SIZE))
    print()
    for tag in ('IROM', 'OCR_RVCT4'):
        s, z = read_block(text, tag)
        if s is None:
            print('  [!!] %-12s 未找到' % tag)
            ok = False
            continue
        good = s.lower() == NEW_START.lower() and z.lower() == NEW_SIZE.lower()
        ok = ok and good
        print('  [%s] %-12s start=%s size=%s' % ('OK' if good else '!!', tag, s, z))

    # VECT_TAB_OFFSET 也要与链接基址一致
    mo = re.search(r'VECT_TAB_OFFSET=(\w+)', text)
    off_good = mo and mo.group(1).lower() == '0xc080'
    ok = ok and bool(off_good)
    print('  [%s] VECT_TAB_OFFSET=%s（应为 0xC080）' % ('OK' if off_good else '!!', mo.group(1) if mo else '缺失'))

    if text != orig and apply_:
        open(PROJ, 'w', encoding='utf-8', newline='').write(text)
        print()
        print('已写入 %s' % os.path.relpath(PROJ, ROOT))
    elif text != orig:
        print()
        print('需要修正（加 --apply 生效）')

    print()
    print('结论:', '已一致' if ok else '不一致，需 --apply 修正')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
