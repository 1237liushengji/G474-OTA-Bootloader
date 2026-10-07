#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用"可读诊断版"替换 BL main.c 里 `#if (BL_BARE_BLINK != 0) ... #else` 之间的整块。

新版特点（针对上次自检的失败原因）：
  * LED 先长亮 1.5 秒作"确认存活"，随后 1Hz 慢闪（上次 15ms 周期肉眼看不见）；
  * 增加全寄存器 USART1(PA9/PA10, AF7, 115200) 输出 3 行 ASCII；
  * **不使用 HSE**，直接 HSI 16MHz —— 若根因是晶振起振失败，这版能直接跑起来。

用法: python ota/scripts/upgrade_bare_blink.py [--apply]
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

MARK_IF = '#if (BL_BARE_BLINK != 0)'
MARK_ELSE = '#else  /* ---------------- 正常 Bootloader ---------------- */'

BODY = r'''#if (BL_BARE_BLINK != 0)

/* ================= 裸机可读诊断固件 =================
 * 回答"MCU 到底有没有在跑、时钟/串口/LED 是否正常"。
 *   * 全寄存器操作，不调用 HAL（最短路径）；
 *   * 不使用 HSE，直接 HSI 16MHz（排除晶振起振失败）；
 *   * LED(PE0,低有效)：先长亮 1.5s 确认存活，随后 1Hz 慢闪；
 *   * USART1(PA9/PA10,AF7) 115200 8N1，输出 3 行 ASCII。
 * ================================================== */

static void Bare_LedInit(void)
{
  RCC->AHB2ENR |= (1UL << 4);                 /* GPIOEEN */
  (void)RCC->AHB2ENR;
  GPIOE->MODER   = (GPIOE->MODER & ~(3UL << 0)) | (1UL << 0);
  GPIOE->OTYPER &= ~(1UL << 0);
  GPIOE->OSPEEDR = (GPIOE->OSPEEDR & ~(3UL << 0)) | (3UL << 0);
  GPIOE->PUPDR   = (GPIOE->PUPDR & ~(3UL << 0));
}

/* 粗略延时 ms。上界由参数传入 + noinline，避免被编译器完全展开
 * （曾因 volatile 常量上界循环被展开，使 main.o 膨胀到 24KB）。 */
__attribute__((noinline))
static void Bare_DelayMs(uint32_t ms)
{
  while (ms-- != 0U)
  {
    uint32_t i = 4000U;                       /* HSI 16MHz 下约 1ms */
    while (i-- != 0U)
    {
      __NOP();
    }
  }
}

static void Bare_UartInit(void)
{
  RCC->AHB2ENR |= (1UL << 0);                 /* GPIOAEN */
  RCC->APB2ENR |= (1UL << 14);                /* USART1EN */
  (void)RCC->APB2ENR;

  /* PA9 = USART1_TX, PA10 = USART1_RX，AF7，推挽，高速 */
  GPIOA->MODER   = (GPIOA->MODER & ~((3UL << 18) | (3UL << 20)))
                 | (2UL << 18) | (2UL << 20);
  GPIOA->OSPEEDR = (GPIOA->OSPEEDR & ~((3UL << 18) | (3UL << 20)))
                 | (3UL << 18) | (3UL << 20);
  GPIOA->PUPDR   = (GPIOA->PUPDR & ~((3UL << 18) | (3UL << 20)))
                 | (1UL << 20);               /* RX 上拉 */
  GPIOA->AFR[1]  = (GPIOA->AFR[1] & ~((0xFUL << 4) | (0xFUL << 8)))
                 | (7UL << 4) | (7UL << 8);

  USART1->CR1 = 0U;
  USART1->CR2 = 0U;
  USART1->CR3 = 0U;
  /* HSI 16MHz, OVER8=0 -> USARTDIV = 16e6/(16*115200) = 8.6806
   * BRR = (8 << 4) | round(0.6806*16) = 0x80 | 11 = 0x8B */
  USART1->BRR = 0x8BU;
  USART1->CR1 = (1UL << 3) | (1UL << 2) | (1UL << 0);   /* TE | RE | UE */
}

static void Bare_Send(const char *s)
{
  while (*s != '\0')
  {
    while ((USART1->ISR & (1UL << 7)) == 0U) { }   /* 等 TXE */
    USART1->TDR = (uint32_t)(uint8_t)(*s);
    s++;
  }
}

int main(void)
{
  Bare_LedInit();
  Bare_UartInit();

  /* 确认存活：长亮 1.5 秒（肉眼一定看得见） */
  GPIOE->BRR = (1UL << 0);
  Bare_DelayMs(1500U);
  GPIOE->BSRR = (1UL << 0);

  Bare_Send("BARE TEST: MCU alive, HSI 16MHz (no HSE)\r\n");
  Bare_Send("BARE TEST: USART1 115200 on PA9\r\n");
  Bare_Send("BARE TEST: led blinks 1Hz from now\r\n");

  for (;;)
  {
    GPIOE->BRR  = (1UL << 0);                 /* 点亮（低有效） */
    Bare_DelayMs(500U);
    GPIOE->BSRR = (1UL << 0);                 /* 熄灭 */
    Bare_DelayMs(500U);
  }
}
'''


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()

    i_if = t.find(MARK_IF)
    i_else = t.find(MARK_ELSE, i_if + 1)
    if i_if < 0 or i_else < 0:
        print('  !! 未找到条件块（#if=%d, #else=%d）' % (i_if, i_else))
        return 2

    body = BODY.replace('\n', '\r\n') if '\r\n' in t else BODY
    t2 = t[:i_if] + body + t[i_else:]

    print('  替换范围: %d ~ %d 字节（旧块 %d 字节 -> 新块 %d 字节）'
          % (i_if, i_else, i_else - i_if, len(body)))
    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(MAIN, 'w', encoding='utf-8', newline='').write(t2)
    print('  已写入')
    return 0


if __name__ == '__main__':
    sys.exit(main())
