#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 BL 从 "after trial" 到跳转之间加**逐步串口打点**，精确定位卡在哪一步。

背景：串口输出停在
    BL: after trial state=0 trialLeft=0 activeSlot=0 lastErr=0x00000000
之后没有任何输出，LED 也不闪（BL_DiagStage 的延时疑似被优化掉，LED 已不可信）。
因此改用**串口**作为唯一可信的定位手段，逐函数打印。

打点覆盖：
    [1] after trial         （已有）
    [2] before DiagInit
    [3] after  DiagInit
    [4] before CanInit
    [5] after  CanInit      （含成功/失败）
    [6] before BootWindow
    [7] after  BootWindow
    [8] before TryBootApp
    [9] TryBootApp 内：校验结果 + 错误码

用法: python ota/scripts/bl_add_trace.py [--apply]
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

PAIRS = [
    # (锚点, 替换)
    ('  /* DIAG 层必须在 ISO-TP 回调可用之前初始化：它决定下载/擦除的目标槽 = 活动槽之外的另一个 */\n  BL_DiagInit(s_activeSlot);',
     '  BL_UART_SEND_STR("TRACE [2] before DiagInit\\r\\n");\n'
     '  /* DIAG 层必须在 ISO-TP 回调可用之前初始化：它决定下载/擦除的目标槽 = 活动槽之外的另一个 */\n'
     '  BL_DiagInit(s_activeSlot);\n'
     '  BL_UART_SEND_STR("TRACE [3] after DiagInit\\r\\n");'),

    ('  /* 5) CAN + ISO-TP 上线，开启动窗口 */\n  s_canOk = (BL_CanInit() == HAL_OK);',
     '  BL_UART_SEND_STR("TRACE [4] before CanInit\\r\\n");\n'
     '  /* 5) CAN + ISO-TP 上线，开启动窗口 */\n'
     '  s_canOk = (BL_CanInit() == HAL_OK);\n'
     '  BL_UART_SEND_STR(s_canOk ? "TRACE [5] CanInit OK\\r\\n" : "TRACE [5] CanInit FAIL\\r\\n");'),

    ('  if (!s_recovery)\n  {\n    BL_BootWindow();\n  }',
     '  if (!s_recovery)\n  {\n'
     '    BL_UART_SEND_STR("TRACE [6] before BootWindow\\r\\n");\n'
     '    BL_BootWindow();\n'
     '    BL_UART_SEND_STR("TRACE [7] after BootWindow\\r\\n");\n'
     '  }'),

    ('    BL_DiagStep(7U, "BL-DIAG 7/7: validating APP image...\\r\\n", 0U);\n    BL_TryBootApp();                              /* 成功则在此跳转，不返回 */',
     '    BL_UART_SEND_STR("TRACE [8] before TryBootApp\\r\\n");\n'
     '    BL_TryBootApp();                              /* 成功则在此跳转，不返回 */\n'
     '    BL_UART_SEND_STR("TRACE [9] TryBootApp returned (未跳转)\\r\\n");'),
]


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()
    if 'TRACE [2]' in t:
        print('  打点已存在，跳过')
        return 0

    ok = 0
    for old, new in PAIRS:
        hit = False
        for conv in (lambda s: s, lambda s: s.replace('\n', '\r\n')):
            o, n = conv(old), conv(new)
            if t.count(o) == 1:
                t = t.replace(o, n, 1)
                ok += 1
                hit = True
                break
        if not hit:
            print('  !! 锚点未匹配: %s' % old.strip().split('\n')[0][:60])

    print('  已插入 %d/%d 处打点' % (ok, len(PAIRS)))
    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(MAIN, 'w', encoding='utf-8', newline='').write(t)
    print('  已写入 %s' % os.path.relpath(MAIN, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
