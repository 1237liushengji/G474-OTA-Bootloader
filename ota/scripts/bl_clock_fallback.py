#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
让 BL 的时钟配置**在 HSE 起振失败时自动回落到 HSI**，并上报实际时钟源。

背景（实测）：裸机诊断固件（HSI 16MHz、不用 HSE）完全正常：
串口 820 字节、LED 1-2-3 心跳；而 BL/APP 依赖 HSE 8MHz -> PLL 150MHz，
`bl_clock.c` 里 HSE 失败直接 `Error_Handler()` 死循环 —— 什么都不输出。

本脚本把"直接死"改成"回落 + 上报"：
  * HSE + PLL -> 150MHz（原行为）；
  * HSE 起振失败 -> 自动改用 HSI + PLL -> 约 137MHz（仍在 170MHz 上限内）；
  * 通过 BL_ClockSource() 把实际结果告诉 main，由 main 在 UART 就绪后打印。

这样无论晶振好坏，设备都能起来并**自己说明用的是哪个时钟源**——
把"是晶振坏了还是我代码错了"这个悬案一次问清。

用法: python ota/scripts/bl_clock_fallback.py [--apply]
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
CLOCK_C = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'bl_clock.c')
CLOCK_H = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Inc', 'bl_clock.h')

OLD = '''  /** 振荡器：HSE + PLL */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState       = RCC_HSE_ON;
  RCC_OscInitStruct.PLL.PLLState   = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource  = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM       = RCC_PLLM_DIV2;
  RCC_OscInitStruct.PLL.PLLN       = 75;
  RCC_OscInitStruct.PLL.PLLP       = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ       = RCC_PLLQ_DIV2;
  RCC_OscInitStruct.PLL.PLLR       = RCC_PLLR_DIV2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }'''

NEW = '''  /** 振荡器：优先 HSE + PLL -> 150MHz；HSE 起振失败则回落 HSI + PLL。
   *
   * 为什么要回落：实测本板 HSE(8MHz) 起振失败时，原实现直接进 Error_Handler
   * 死循环——串口无输出、LED 无反应，现场完全无法诊断（表现为"上电没反应"）。
   * 改成回落后，无论晶振好坏设备都能起来，并能上报实际使用的时钟源。
   */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState       = RCC_HSE_ON;
  RCC_OscInitStruct.PLL.PLLState   = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource  = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM       = RCC_PLLM_DIV2;
  RCC_OscInitStruct.PLL.PLLN       = 75;
  RCC_OscInitStruct.PLL.PLLP       = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ       = RCC_PLLQ_DIV2;
  RCC_OscInitStruct.PLL.PLLR       = RCC_PLLR_DIV2;

  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    /* ---- HSE 路径失败：回落到 HSI + PLL ---- */
    s_clkSource = BL_CLK_HSI_FALLBACK;

    RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
    RCC_OscInitStruct.HSIState       = RCC_HSI_ON;
    RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
    RCC_OscInitStruct.PLL.PLLState   = RCC_PLL_ON;
    RCC_OscInitStruct.PLL.PLLSource  = RCC_PLLSOURCE_HSI;
    RCC_OscInitStruct.PLL.PLLM       = RCC_PLLM_DIV4;   /* HSI16 / 4 = 4MHz */
    RCC_OscInitStruct.PLL.PLLN       = 86;              /* x86 = 344MHz VCO  */
    RCC_OscInitStruct.PLL.PLLP       = RCC_PLLP_DIV2;
    RCC_OscInitStruct.PLL.PLLQ       = RCC_PLLQ_DIV2;
    RCC_OscInitStruct.PLL.PLLR       = RCC_PLLR_DIV2;   /* /2 = 172MHz -> 略超 */
    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
    {
      Error_Handler();                                  /* HSI 也失败：真的没救了 */
    }
  }
  else
  {
    s_clkSource = BL_CLK_HSE;
  }'''

# HSI 回落的 PLL 参数必须落在合法窗口内，并与 HSE 路径同频（串口时序才一致）：
#   HSI16 / M=4 = 4MHz     （VCO 输入需 2.66~16MHz）
#   4MHz x N=75 = 300MHz   （VCO 输出需 96~344MHz）
#   300MHz / R=2 = 150MHz  （= HSE 路径同频）
NEW = NEW.replace('PLLN       = 86;              /* x86 = 344MHz VCO  */',
                  'PLLN       = 75;              /* x75 = 300MHz VCO（96~344 合法）*/')
NEW = NEW.replace('/* /2 = 172MHz -> 略超 */',
                  '/* /2 = 150MHz，与 HSE 路径同频 */')

GLOBALS = '''
/* ---- 实际使用的时钟源（供 main 在 UART 就绪后上报） ---- */
BL_ClkSource_t s_clkSource = BL_CLK_UNKNOWN;

BL_ClkSource_t BL_ClockSource(void)
{
  return s_clkSource;
}
'''

H_DECL = '''
/** 系统时钟来源（诊断用：区分"晶振正常"与"已回落 HSI"） */
typedef enum
{
  BL_CLK_UNKNOWN      = 0,
  BL_CLK_HSE          = 1,   /* HSE 8MHz + PLL -> 150MHz（正常） */
  BL_CLK_HSI_FALLBACK = 2    /* HSE 起振失败，已回落 HSI + PLL */
} BL_ClkSource_t;

BL_ClkSource_t BL_ClockSource(void);
'''


def main():
    apply_ = '--apply' in sys.argv
    c = open(CLOCK_C, encoding='utf-8', errors='replace').read()
    h = open(CLOCK_H, encoding='utf-8', errors='replace').read()

    if 'BL_CLK_HSI_FALLBACK' in c:
        print('  回落逻辑已存在，跳过 bl_clock.c')
    else:
        done = False
        for conv in (lambda s: s, lambda s: s.replace('\n', '\r\n')):
            o, n = conv(OLD), conv(NEW)
            if c.count(o) == 1:
                c = c.replace(o, n, 1)
                done = True
                break
        if not done:
            print('  !! bl_clock.c 锚点匹配失败（CRLF=%d, LF=%d）'
                  % (c.count(OLD.replace('\n', '\r\n')), c.count(OLD)))
            return 2
        # 在 SystemClock_Config 之前插入全局变量与访问器
        anchor = 'void SystemClock_Config(void)'
        if c.count(anchor) != 1:
            print('  !! SystemClock_Config 锚点匹配 %d 次' % c.count(anchor))
            return 2
        c = c.replace(anchor, GLOBALS.replace('\n', '\r\n').lstrip('\r\n') + anchor, 1)
        print('  已加入 HSE 失败回落 HSI + 时钟源上报')

    if 'BL_ClkSource_t' not in h:
        # 插到最后一个 #endif 之前
        idx = h.rfind('#endif')
        if idx < 0:
            print('  !! bl_clock.h 未找到 #endif'); return 2
        h = h[:idx] + H_DECL.replace('\n', '\r\n').lstrip('\r\n') + '\r\n' + h[idx:]
        print('  已加入 BL_ClkSource_t 声明')

    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(CLOCK_C, 'w', encoding='utf-8', newline='').write(c)
    open(CLOCK_H, 'w', encoding='utf-8', newline='').write(h)
    print('  已写入 bl_clock.c / bl_clock.h')
    return 0


if __name__ == '__main__':
    sys.exit(main())
