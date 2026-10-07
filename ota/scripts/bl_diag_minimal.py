#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 BL 的分步自检改成**纯 LED 最小版**。

原因（实测）：带字符串日志的版本会通过 BL_UART_SEND_STR 拉进完整 printf 库
（printf1.o/printf2.o/_printf_ll.o/bigflt0.o/btod.o/rt_locale_intlibspace.o），
链接报 L6407E "Sections of aggregate size 0x2390 (9112) bytes could not fit"，
BL 区超出约 9KB。

最小版只做 GPIO 寄存器操作（PE0 闪 N 次），不引入任何库函数，
预计增量 < 500 字节，可稳稳落在现有 46KB 预算内。

用法: python ota/scripts/bl_diag_minimal.py [--apply]
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

# 通用：把 BL_DiagStep(...) 降级为 BL_DiagStage(同一步号, 同一 offMs)
STEP_RE = re.compile(r'BL_DiagStep\(\s*(\d+)U\s*,\s*"[^"]*"\s*,\s*([^)]+)\)')

# 删除已插入的字符串常量参数遗留：把 BL_DiagStage 的多余调用保留即可


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()
    orig = t

    if 'BL_DiagStep' not in t:
        print('  已是纯 LED 版本，无需改动')
        return 0

    t, n = STEP_RE.subn(lambda m: 'BL_DiagStage(%sU, %s)' % (m.group(1), m.group(2)), t)
    print('  降级 BL_DiagStep -> BL_DiagStage：%d 处' % n)

    # 删除 BL_DiagStep 函数定义
    fn = re.search(r'\n/\* 打一条步骤日志.*?\n\}\n', t, re.S)
    if fn:
        t = t[:fn.start()] + '\n' + t[fn.end():]
        print('  已删除 BL_DiagStep 函数定义')
    else:
        print('  [!] 未找到 BL_DiagStep 函数定义，请手工检查')

    if 'BL_DiagStep' in t:
        print('  !! 仍有 BL_DiagStep 残留，未写入')
        return 2

    if t != orig and apply_:
        open(MAIN, 'w', encoding='utf-8', newline='').write(t)
        print('  已写入')
    elif t != orig:
        print('  干跑（加 --apply 生效）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
