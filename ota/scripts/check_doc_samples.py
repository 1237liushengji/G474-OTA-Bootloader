#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 ota/docs/06 里的诊断报文样本是否与契约一致（防文档写错字节）。"""
import os
import re
import struct
import sys

from paths import workspace_root

ROOT = workspace_root()


def contract():
    t = open(os.path.join(ROOT, 'ota', 'common', 'ota_common.h'), encoding='utf-8', errors='replace').read()
    raw = dict(re.findall(r'^[ \t]*#define[ \t]+([A-Za-z_]\w*)[ \t]+([^\r\n/]+)', t, re.M))
    v = {}
    for _ in range(8):
        prog = False
        for k, e in raw.items():
            e = re.sub(r'\b([A-Za-z_]\w*)\b', lambda m: str(v[m.group(1)]) if m.group(1) in v else m.group(0), e)
            e = re.sub(r'([0-9A-Fa-fxX]+)[uUlL]+\b', r'\1', e)
            if not re.fullmatch(r'[0-9A-Fa-fxX()\s+\-*/%]+', e):
                continue
            try:
                v[k] = int(eval(e, {"__builtins__": {}}, {})); prog = True
            except Exception:
                pass
        if not prog:
            break
    return v


def hexs(b):
    return " ".join("%02X" % x for x in b)


def main():
    c = contract()
    slot_a = c['OTA_SLOT_A_ADDR'] + c['OTA_IMG_HDR_SIZE']
    slot_b = c['OTA_SLOT_B_ADDR'] + c['OTA_IMG_HDR_SIZE']
    page = c['OTA_FLASH_PAGE_SIZE']
    slot_size = c['OTA_SLOT_SIZE']

    def req34(addr, size):
        return bytes([0x34, 0x00, 0x44]) + struct.pack(">I", addr) + struct.pack(">I", size)

    print("契约: Slot A APP=%08X  Slot B APP=%08X  页=%d  槽=%d" % (slot_a, slot_b, page, slot_size))
    print()
    checks = [
        ("3.1 进扩展会话", "10 03", "50 03 00 32 01 F4"),
        ("3.2 读能力位图", "22 F1 84", "62 F1 84 07"),
        ("3.3 读活动槽", "22 F1 81", "62 F1 81 00"),
        ("3.4 请求写 BL 区 0x08000000", hexs(req34(0x08000000, 0x1000)), "应回 NRC 7F 34 31"),
        ("3.5 请求写活动槽 Slot A", hexs(req34(slot_a, 0x1000)), "应回 NRC 7F 34 22"),
        ("(参考) 请求写 Slot B", hexs(req34(slot_b, 0x1000)), "合法 -> 正响应 74 20 03 FD"),
        ("3.6 块序号 0", "36 00 AA BB", "应回 NRC 7F 36 73"),
        ("3.7 请求进 BL", "2E F1 90 01", "6E F1 90"),
    ]
    for name, tx, expect in checks:
        print("  %-34s 发: %-46s %s" % (name, tx, expect))

    print()
    print("=== 与文档正文逐一比对 ===")
    doc = open(os.path.join(ROOT, 'ota', 'docs', '06-首次烧录与上板验证步骤.md'),
               encoding='utf-8', errors='replace').read()
    want = [
        ('3.4 请求字节', "34 00 44 08 00 00 00 00 00 10 00"),
        ('3.5 请求字节', hexs(req34(slot_a, 0x1000))),
        ('Slot B 打包地址', "0x08040880"),
        ('会话正响应', "50 03 00 32 01 F4"),
    ]
    bad = 0
    for label, s in want:
        ok = s in doc
        if not ok:
            bad += 1
        print("  [%s] %-16s %s" % ("OK" if ok else "!!", label, s))

    # 额外：块数与页数
    payload = slot_size - c['OTA_IMG_HDR_SIZE']
    blocks = -(-payload // 1021)
    pages = slot_size // page
    print()
    print("  期望块数 = ceil(%d/1021) = %d   期望页数 = %d" % (payload, blocks, pages))
    for label, s in (('209 个块', "%d 个 `0x36` 块" % blocks), ('104 页', "≈ %d 页" % pages)):
        ok = s in doc
        if not ok:
            bad += 1
        print("  [%s] %s" % ("OK" if ok else "!!", label))

    print()
    print("结论:", "文档样本与契约一致" if bad == 0 else "有 %d 处不符，需修正文档" % bad)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
