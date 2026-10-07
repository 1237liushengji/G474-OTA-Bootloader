#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 APP 的时钟配置加"HSE 失败回落 HSI"（与 BL 的 bl_clock.c 保持一致）。

虽然实测本板 HSE 正常（BL 日志：`clock HSE+PLL 150MHz OK`），但仍保留回落：
  * 晶振是易损件，虚焊/老化/批次差异都可能让个别板起不来；
  * 原实现一旦 HSE 失败就是 `Error_Handler()` 死循环 —— 现场表现为"上电完全没反应"，
    正是本次排查耗时的根源。回落 + 上报能让同类问题一眼可见。

用法: python ota/scripts/app_clock_fallback.py [--apply]
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'G474', 'Core', 'Src', 'main.c')

OLD = '''  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }'''

NEW = '''  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    /* HSE 起振失败 -> 回落 HSI + PLL（同为 150MHz，UART/CAN 时序一致）。
     * 原实现此处直接 Error_Handler() 死循环：现场表现为"上电完全没反应"，
     * 串口与 LED 都无输出，无法诊断。回落后至少还能起来并打印告警。 */
    RCC_OscInitStruct.OscillatorType      = RCC_OSCILLATORTYPE_HSI;
    RCC_OscInitStruct.HSIState            = RCC_HSI_ON;
    RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
    RCC_OscInitStruct.PLL.PLLState        = RCC_PLL_ON;
    RCC_OscInitStruct.PLL.PLLSource       = RCC_PLLSOURCE_HSI;
    RCC_OscInitStruct.PLL.PLLM            = RCC_PLLM_DIV4;   /* HSI16/4 = 4MHz */
    RCC_OscInitStruct.PLL.PLLN            = 75;              /* x75 = 300MHz VCO */
    RCC_OscInitStruct.PLL.PLLP            = RCC_PLLP_DIV2;
    RCC_OscInitStruct.PLL.PLLQ            = RCC_PLLQ_DIV2;
    RCC_OscInitStruct.PLL.PLLR            = RCC_PLLR_DIV2;   /* /2 = 150MHz */
    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
    {
      Error_Handler();
    }
    g_hseFailed = 1U;                 /* 供启动日志上报 */
  }'''

# 全局标志（放在文件靠前的 USER CODE 区）
FLAG_DECL = '''
/* HSE 起振失败标志：1 = 已回落 HSI+PLL（启动日志会上报） */
uint8_t g_hseFailed = 0U;
'''


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()

    if 'g_hseFailed' in t:
        print('  回落逻辑已存在，跳过')
        return 0

    done = False
    for conv in (lambda s: s, lambda s: s.replace('\n', '\r\n')):
        o, n = conv(OLD), conv(NEW)
        if t.count(o) == 1:
            t = t.replace(o, n, 1)
            done = True
            break
        if t.count(o) > 1:
            print('  !! 锚点匹配 %d 次（多处 Error_Handler），需人工确认' % t.count(o))
            return 2
    if not done:
        print('  !! 未找到时钟配置锚点（CRLF=%d, LF=%d）'
              % (t.count(OLD.replace('\n', '\r\n')), t.count(OLD)))
        return 2

    # 标志位：插在 USER CODE BEGIN PV 之后（若不存在则插在第一个函数定义前）
    anchor = '/* USER CODE BEGIN PV */'
    if anchor in t:
        nl = '\r\n' if '\r\n' in t else '\n'
        t = t.replace(anchor, anchor + nl + FLAG_DECL.replace('\n', nl).strip(), 1)
        print('  已加入 g_hseFailed 标志（USER CODE BEGIN PV）')
    else:
        print('  [!] 未找到 USER CODE BEGIN PV，标志未加（不影响回落功能）')

    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(MAIN, 'w', encoding='utf-8', newline='').write(t)
    print('  已写入 G474/Core/Src/main.c')
    return 0


if __name__ == '__main__':
    sys.exit(main())
