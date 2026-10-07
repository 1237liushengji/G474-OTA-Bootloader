#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成"裸机闪灯自检"固件：用于把 环境问题 与 软件问题 彻底分开。

现象背景：上电后 LED 一下都不闪、LCD 无背光、串口 R:0。
LED 引脚已核实为 PE0（低有效），所以"不闪"意味着代码几乎没在执行。

本自检做的事情（刻意最少）：
  1) 开 GPIOE 时钟；
  2) PE0 配为推挽输出；
  3) 死循环：拉低 -> 延时 -> 拉高 -> 延时。
**不调用 HAL、不做时钟配置、不初始化任何外设**（除 GPIOE 时钟）。

判据：
  * LED 以约 2Hz 闪烁  -> MCU 在跑、LED 与引脚正确、烧录链路正常
                          => 问题在 BL/APP 的初始化路径（时钟/看门狗/跳转）
  * LED 仍完全不亮      -> 环境问题：供电、BOOT0、SWD 连接、
                          调试器把芯片按住、或芯片/板子故障
                          => 与 OTA 代码无关，先修硬件链路

用法: python ota/scripts/make_bare_blink.py [--apply]
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')
PROJ = os.path.join(ROOT, 'ota', 'bootloader', 'MDK-ARM', 'BL.uvprojx')

GATE = 'BL_BARE_BLINK'

BLOCK = '''
/* ============ 裸机闪灯自检（BL_BARE_BLINK=1 时生效） ============
 * 上电后 LED(PE0) 约 2Hz 闪烁，用于判定"MCU 到底有没有在执行代码"。
 * 刻意不调用 HAL / 不配置时钟 / 不初始化外设，把变量降到最低。
 * ============================================================== */
#if (BL_BARE_BLINK != 0)

int main(void)
{
  /* 1) 开 GPIOE 时钟（RCC_AHB2ENR bit4 = GPIOEEN） */
  RCC->AHB2ENR |= (1UL << 4);
  (void)RCC->AHB2ENR;                       /* 读回，确保时钟已使能 */

  /* 2) PE0 推挽输出（MODER bit[1:0] = 01），无上下拉，高速 */
  GPIOE->MODER   = (GPIOE->MODER & ~(3UL << 0)) | (1UL << 0);
  GPIOE->OTYPER &= ~(1UL << 0);
  GPIOE->OSPEEDR = (GPIOE->OSPEEDR & ~(3UL << 0)) | (3UL << 0);
  GPIOE->PUPDR   = (GPIOE->PUPDR & ~(3UL << 0));

  for (;;)
  {
    GPIOE->BRR  = (1UL << 0);               /* 拉低：点亮（低有效） */
    for (volatile uint32_t d = 0U; d < 200000U; d++) { __NOP(); }
    GPIOE->BSRR = (1UL << 0);               /* 拉高：熄灭 */
    for (volatile uint32_t d = 0U; d < 200000U; d++) { __NOP(); }
  }
}

#else  /* ---------------- 正常 Bootloader ---------------- */


'''


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()

    if GATE in t:
        print('  裸机闪灯块已存在')
    else:
        anchor = 'int main(void)\n{'
        # 找 Bootloader 的 main（在文件里第一个）
        if t.count(anchor) != 1:
            print('  !! main 锚点匹配 %d 次，无法安全插入' % t.count(anchor))
            return 2
        t = t.replace(anchor, BLOCK.replace('\n', '\r\n').lstrip('\r\n') + anchor, 1)
        print('  已插入裸机闪灯块')

    # 文件末尾补 #endif（若尚未补）
    tail_anchor = '/* USER CODE END 4 */'
    if '#endif /* BL_BARE_BLINK */' not in t:
        if t.count(tail_anchor) == 1:
            t = t.replace(tail_anchor, tail_anchor + '\r\n\r\n#endif /* BL_BARE_BLINK */', 1)
        else:
            t = t.rstrip() + '\r\n\r\n#endif /* BL_BARE_BLINK */\r\n'
        print('  已补 #endif')

    # 添加开关宏（默认 0 = 正常 BL）
    if '#define BL_BARE_BLINK' not in t:
        macro = '#define BL_BARE_BLINK       0    /* 置 1 = 只做裸机闪灯自检，便于隔离环境问题 */'
        am = '#define BL_DIAG_LED'
        if am in t:
            t = t.replace(am, macro + '\r\n' + am, 1)
            print('  已加入 BL_BARE_BLINK 开关（默认 0）')
        else:
            print('  !! 未找到宏插入点')
            return 2

    if apply_:
        open(MAIN, 'w', encoding='utf-8', newline='').write(t)
        print('  已写入 main.c')
    else:
        print('  干跑（加 --apply 生效）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
