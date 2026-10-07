#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双工程产物核对：确认 BL 与 APP 的链接地址、体积、以及分区表一致性。

这是"改完必跑"的收尾检查——因为 Keil 会在打开/保存工程时重写 .uvprojx 与它生成的 .sct，
曾多次把链接地址还原，导致"编译通过但地址错误"。本脚本把这件事变成可复核的一步。
"""
import os

from paths import workspace_root
import re
import sys

ROOT = workspace_root()


def find_map(part):
    hits = []
    for dirpath, _d, files in os.walk(ROOT):
        if '_baseline_backup' in dirpath or 'src_ref' in dirpath:
            continue
        for fn in files:
            if fn == part:
                hits.append(os.path.join(dirpath, fn))
    return hits


def read_vectors(map_path):
    t = open(map_path, encoding='utf-8', errors='replace').read()
    vb = re.search(r'__Vectors\s+(0x[0-9a-fA-F]+)', t)
    ve = re.search(r'__Vectors_End\s+(0x[0-9a-fA-F]+)', t)
    ro = re.search(r'Total RO\s+Size \(Code \+ RO Data\)\s+(\d+)', t)
    rw = re.search(r'Total RW\s+Size \(RW Data \+ ZI Data\)\s+(\d+)', t)
    return (vb.group(1) if vb else None,
            ve.group(1) if ve else None,
            int(ro.group(1)) if ro else None,
            int(rw.group(1)) if rw else None)


def read_contract():
    """从 ota_common.h 求值关键地址常量"""
    p = os.path.join(ROOT, 'ota', 'common', 'ota_common.h')
    t = open(p, encoding='utf-8', errors='replace').read()
    raw = dict(re.findall(r'^[ \t]*#define[ \t]+([A-Za-z_]\w*)[ \t]+([^\r\n/]+)', t, re.M))
    vals = {}
    for _ in range(8):
        prog = False
        for k, e in raw.items():
            e = re.sub(r'\b([A-Za-z_]\w*)\b', lambda m: str(vals[m.group(1)]) if m.group(1) in vals else m.group(0), e)
            e = re.sub(r'([0-9A-Fa-fxX]+)[uUlL]+\b', r'\1', e)
            if not re.fullmatch(r'[0-9A-Fa-fxX()\s+\-*/%]+', e):
                continue
            try:
                vals[k] = int(eval(e, {"__builtins__": {}}, {})); prog = True
            except Exception:
                pass
        if not prog:
            break
    return vals


def main():
    c = read_contract()
    need = ['OTA_BL_CODE_ADDR', 'OTA_BL_CODE_SIZE', 'OTA_SLOT_A_ADDR', 'OTA_SLOT_SIZE',
            'OTA_SLOT_B_ADDR', 'OTA_IMG_HDR_SIZE', 'OTA_FLASH_BASE']
    miss = [k for k in need if k not in c]
    if miss:
        print('!! 契约常量缺失: %s' % miss); return 2

    exp_bl = c['OTA_FLASH_BASE']                      # BL 向量表必须 0x08000000
    exp_app = c['OTA_SLOT_A_ADDR'] + c['OTA_IMG_HDR_SIZE']
    exp_bl_limit = c['OTA_BL_CODE_ADDR'] + c['OTA_BL_CODE_SIZE']
    exp_slot_limit = c['OTA_SLOT_A_ADDR'] + c['OTA_SLOT_SIZE']

    print('=== 契约期望 ===')
    print('  BL 向量表      : 0x%08X' % exp_bl)
    print('  BL 区上界      : 0x%08X  (代码必须落在此前)' % exp_bl_limit)
    print('  APP 向量表     : 0x%08X' % exp_app)
    print('  Slot A 上界    : 0x%08X' % exp_slot_limit)
    print()

    bad = 0
    # map 的定位：BL 在 ota/bootloader/MDK-ARM/ 下，APP 在 G474/MDK-ARM/ 下。
    # 用"期望向量表地址"反查更稳：读每个候选 map 的 __Vectors，取与期望一致的那个；
    # 若都不一致，再退化为按 mtime 取最新，从而能报出"地址错"而不是"找不到"。
    for label, mapname, exp_vec, limit, base in (
            ('BL ', 'BL.map', exp_bl, exp_bl_limit, c['OTA_FLASH_BASE']),
            ('APP', 'G474.map', exp_app, exp_slot_limit, c['OTA_SLOT_A_ADDR'])):
        cands = find_map(mapname)
        if not cands:
            print('[%s] !! 未找到 %s（是否还没构建？）' % (label, mapname)); bad += 1; continue
        m = None
        for cand in cands:
            v, _ve, _ro, _rw = read_vectors(cand)
            if v is not None and int(v, 16) == exp_vec:
                m = cand
                break
        if m is None:
            m = max(cands, key=os.path.getmtime)
        vec, vend, ro, rw = read_vectors(m)
        ok_addr = (vec is not None) and (int(vec, 16) == exp_vec)
        flash_use = (ro or 0) + (rw or 0)
        ok_size = flash_use <= (limit - (c['OTA_FLASH_BASE'] if label == 'BL ' else c['OTA_SLOT_A_ADDR']))
        print('[%s] %s' % (label, m.replace(ROOT + os.sep, '')))
        print('      向量表 = %s   期望 0x%08X   %s' % (vec, exp_vec, 'OK' if ok_addr else '!! 不符'))
        print('      RO=%s RW=%s  占用 %d 字节  上界余量 %d 字节  %s'
              % (ro, rw, flash_use, limit - base - flash_use,
                 'OK' if ok_size else '!! 超出'))
        if not ok_addr or not ok_size:
            bad += 1
        print()

    print('=' * 68)
    print('结论:', 'BL 与 APP 的链接地址/体积均与分区表一致' if bad == 0
          else '有 %d 项不符，必须先修正再烧录' % bad)
    print('=' * 68)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
