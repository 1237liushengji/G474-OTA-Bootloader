#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 APP 加**启动分段打点** —— 用于定位"BL 跳转成功但 APP 起不来"。

背景：APP 目前除 CAN 自检外没有启动日志。若它在 HAL_Init / 时钟 / LCD 初始化
处卡住（LCD_Init 内有约 760ms 延时，且屏幕未接时行为未知），外部完全看不出来。

打点覆盖 APP 启动的关键阶段，把"卡在哪"变成可观察的串口输出：
    APP [1] main entry
    APP [2] HAL_Init done
    APP [3] clock OK (HSE or HSI fallback)
    APP [4] MX_GPIO_Init done
    APP [5] CAN init done
    APP [6] Uds/Ota init done
    APP [7] LCD_Init done
    APP [8] IWDG started, entering main loop

用法: python ota/scripts/app_add_trace.py [--apply]
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'G474', 'Core', 'Src', 'main.c')

# 锚点 -> (插入内容, 插在前面还是后面)
PAIRS = [
    ('  HAL_Init();',
     '  BSP_UART_Send("APP [1] main entry\\r\\n", 20U);'),

    ('  SystemClock_Config();',
     '  BSP_UART_Send("APP [2] HAL_Init done\\r\\n", 26U);'),

    ('  MX_GPIO_Init();',
     '  BSP_UART_Send(g_hseFailed ? "APP [3] clock OK (HSI fallback)\\r\\n"\n'
     '                             : "APP [3] clock OK (HSE 150MHz)\\r\\n", 32U);'),

    ('  if (CAN_Init() != HAL_OK)',
     '  BSP_UART_Send("APP [4] GPIO init done\\r\\n", 26U);'),

    ('  Uds_Init();',
     '  BSP_UART_Send("APP [5] CAN self-test done\\r\\n", 31U);'),

    ('  LCD_Init();',
     '  BSP_UART_Send("APP [6] Uds/Ota init done\\r\\n", 29U);'),

    ('  hiwdg.Instance     = IWDG;',
     '  BSP_UART_Send("APP [7] LCD_Init done\\r\\n", 26U);'),

    ('  while (1)\n  {\n    App_Process();',
     '  BSP_UART_Send("APP [8] entering main loop\\r\\n", 30U);'),
]


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()

    if 'APP [1] main entry' in t:
        print('  APP 打点已存在，跳过')
        return 0

    # 打点必须插在锚点**之前**（因为锚点本身是那一步的动作）
    ok = 0
    for anchor, trace in PAIRS:
        for conv in (lambda s: s, lambda s: s.replace('\n', '\r\n')):
            a, tr = conv(anchor), conv(trace)
            if t.count(a) == 1:
                indent = '  '
                t = t.replace(a, tr + '\n' + indent + a.lstrip(), 1)
                ok += 1
                break

    print('  已插入 %d/%d 处打点' % (ok, len(PAIRS)))
    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(MAIN, 'w', encoding='utf-8', newline='').write(t)
    print('  已写入 %s' % os.path.relpath(MAIN, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
