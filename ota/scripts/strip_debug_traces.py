#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理排查期间加入的临时打点，让 BL 与 APP 回到"发布形态"。

保留的内容（有长期价值）：
  * BL 的裸机诊断固件骨架（BL_BARE_BLINK，默认 0 = 关闭）
  * BL 的 7 步 LED 分步自检（现场无串口时的定位手段）
  * APP 的早期 LED 打点（HAL/时钟在串口之前，卡那里无输出）
  * BL/APP 的 HSE 失败回落 HSI（真实的健壮性改进）

移除的内容（仅用于一次性排查）：
  * BL 里所有 "TRACE ..." 串口打点
  * APP 里 "APP-LED 1/2/3 passed" 汇总行（打点本身保留，它是启动可见性）

用法: python ota/scripts/strip_debug_traces.py [--apply]
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
BL_MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')
APP_MAIN = os.path.join(ROOT, 'G474', 'Core', 'Src', 'main.c')


def strip_bl(t):
    """删除 BL 中所有以 TRACE 开头的串口打点语句及其专属注释块。"""
    lines = t.split('\n')
    out = []
    removed = 0
    skip_block = 0
    for ln in lines:
        s = ln.strip()
        if 'TRACE' not in s and skip_block == 0:
            out.append(ln)
            continue
        # 遇到 TRACE 语句或注释 -> 丢弃
        if 'TRACE' in s:
            removed += 1
            # 处理多行语句：到本语句结束（含 ';' 或 ')' 收尾）为止
            if s.startswith('/*') and not s.endswith('*/'):
                skip_block = 1
            elif not (s.endswith(';') or s.endswith(')') or s.endswith(')')):
                skip_block = 1
            continue
        if skip_block:
            removed += 1
            if s.endswith('*/') or s.endswith(';') or s.endswith(')') or s.endswith(')'):
                skip_block = 0
            continue
        out.append(ln)
    return '\n'.join(out), removed


def main():
    apply_ = '--apply' in sys.argv

    # ---- BL ----
    t = open(BL_MAIN, encoding='utf-8', errors='replace').read()
    t2, n = strip_bl(t)
    t2 = re.sub(r'\n{3,}', '\n\n', t2)
    print('  BL: 移除 %d 行 TRACE 打点' % n)

    # ---- APP：移除汇总行（打点本身保留）----
    a = open(APP_MAIN, encoding='utf-8', errors='replace').read()
    old = '  BSP_UART_Send("APP-LED 1/2/3 passed (HAL, clock, uart)\\r\\n", 41U);'
    na = a.replace(old + '\n', '').replace(old, '')
    if na != a:
        print('  APP: 移除汇总行（保留 1/2/3 三处 LED 打点）')
    else:
        print('  APP: 无需改动')

    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(BL_MAIN, 'w', encoding='utf-8', newline='').write(t2)
    open(APP_MAIN, 'w', encoding='utf-8', newline='').write(na)
    print('  已写入')
    return 0


if __name__ == '__main__':
    sys.exit(main())
