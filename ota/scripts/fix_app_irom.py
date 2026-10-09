#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正 APP 工程的链接地址（幂等，支持 Slot A / Slot B）。

问题背景：Keil 在构建时会依据 **OCR_RVCT4**（Linker 页的内存布局）自动生成
分散加载文件 `MDK-ARM/G474/G474.sct`，并**覆盖** `<IROM>` 块里的设置。
因此只改 `<IROM>` 不够 —— 必须同时改 OCR_RVCT4 与 IROM，二者一致才会生效。

【2026-10-09 新增 Slot B 支持】原脚本只硬编码 Slot A，导致
`build_all_firmware.py` 产出的 `G474_B.hex` 与 A **链接地址完全相同**
（都是 0x0800C080），`make_factory_image.py --slot B` 会正确地拒绝它：
  "预期 APP 起始地址 = 0x08040880，实际 = 0x0800C080"
现可用 --slot 切换，Slot B 参数由**契约头推导**而非硬编码，避免血线漂移。

用法:
    python ota/scripts/fix_app_irom.py                     # 检查当前状态（不改动）
    python ota/scripts/fix_app_irom.py --apply             # 修正为 Slot A
    python ota/scripts/fix_app_irom.py --slot B --apply    # 修正为 Slot B
    python ota/scripts/fix_app_irom.py --slot A --apply    # 切回 Slot A
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
PROJ = os.path.join(ROOT, 'G474', 'MDK-ARM', 'G474.uvprojx')
COMMON_H = os.path.join(ROOT, 'ota', 'common', 'ota_common.h')


def _contract_defines():
    """从契约头解析分区常量，避免在脚本里重复硬编码（单一真相源原则）。

    解析不到就返回空 dict，调用方会给出"无法推导"的明确报错，而不是静默用错基址。
    """
    if not os.path.isfile(COMMON_H):
        return {}
    text = open(COMMON_H, encoding='utf-8', errors='replace').read()
    out = {}
    for m in re.finditer(r'#define\s+(OTA_[A-Z0-9_]+)\s+(0[xX][0-9A-Fa-f]+|\d+)[uU]?\b', text):
        try:
            out[m.group(1)] = int(m.group(2), 0)
        except ValueError:
            pass
    return out


def slot_params(slot):
    """返回 (start_addr, size, vect_off_hex) ；推导不出来就抛 ValueError。

    槽大小用契约的 **OTA_SLOT_SIZE**（两槽共用，= 208KB），不要用
    "B_ADDR - A_ADDR" 推 —— 那个式子会把 Bank1 剩余空间也算进去，
    算出 0x34780 这种比契约大 2KB 的错值。
    """
    v = _contract_defines()
    hdr = v.get('OTA_IMG_HDR_SIZE')
    size = v.get('OTA_SLOT_SIZE')
    base = v.get('OTA_SLOT_A_ADDR') if slot == 'A' else v.get('OTA_SLOT_B_ADDR')
    if not base or not hdr or not size:
        raise ValueError('无法从契约头推导 Slot %s 参数'
                         '（需 OTA_SLOT_%s_ADDR / OTA_IMG_HDR_SIZE / OTA_SLOT_SIZE）'
                         % (slot, slot))
    return base + hdr, size, '0x%X' % ((base + hdr) & 0xFFFF)


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
    slot = 'B' if '--slot' in sys.argv and sys.argv[sys.argv.index('--slot') + 1].upper() == 'B' else 'A'

    try:
        app, size, vect_off = slot_params(slot)
    except ValueError as e:
        print('[!!] %s' % e)
        return 2
    except IndexError:
        print('[!!] --slot 需要一个参数（A 或 B）')
        return 2

    # Keil 的 StartAddress 是去掉最高位 0x0 的写法：0x0800C080 -> 0x800C080
    new_start = '0x%X' % (app & 0x0FFFFFFF)
    new_size = '0x%X' % size

    text = open(PROJ, encoding='utf-8', errors='replace').read()
    orig = text

    for tag in ('IROM', 'OCR_RVCT4'):
        text, _ = patch_block(text, tag, new_start, new_size)

    # VECT_TAB_OFFSET 必须与链接基址一致：VTOR 要 0x80 对齐（血线：VTOR[6:0] 是 RES0）。
    # 该值 = 槽基址的低 24 位（如 Slot B -> 0x40800），Keil 侧写作 0x40800。
    # 匹配用 [0-9A-Fa-fxX]+ 而不是 \w+：\w 不含 'x'，会漏掉 "0xC080" 里的 x
    text, n_off = re.subn(r'VECT_TAB_OFFSET=[0-9A-Fa-fxX]+',
                          'VECT_TAB_OFFSET=%s' % vect_off, text)

    ok = True
    print('目标: APP 链接基址 0x%08X  大小 0x%X (%d 字节)  [Slot %s]'
          % (app, size, size, slot))
    print()
    for tag in ('IROM', 'OCR_RVCT4'):
        s, z = read_block(text, tag)
        if s is None:
            print('  [!!] %-12s 未找到' % tag)
            ok = False
            continue
        good = s.lower() == new_start.lower() and z.lower() == new_size.lower()
        ok = ok and good
        print('  [%s] %-12s start=%s size=%s' % ('OK' if good else '!!', tag, s, z))

    mo = re.search(r'VECT_TAB_OFFSET=([0-9A-Fa-fxX]+)', text)
    off_good = bool(mo) and mo.group(1).upper() == vect_off.upper()
    ok = ok and off_good
    print('  [%s] VECT_TAB_OFFSET=%s（应为 %s）'
          % ('OK' if off_good else '!!', mo.group(1) if mo else '缺失', vect_off))
    if n_off == 0:
        print('  [!!] VECT_TAB_OFFSET 替换未命中（模式未匹配）——'
              '若显示 OK 但工程里其实没 0x 前缀，编译会报 invalid digit')

    if text != orig and apply_:
        open(PROJ, 'w', encoding='utf-8', newline='').write(text)
        print()
        print('已写入 %s' % os.path.relpath(PROJ, ROOT))
        print('⚠️  必须紧接着重新编译，否则 .sct 仍是旧基址（Keil 每次构建都会重生成 sct）')
    elif text != orig:
        print()
        print('需要修正（加 --apply 生效）')

    print()
    print('结论:', '已一致' if ok else '不一致，需 --apply 修正')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
