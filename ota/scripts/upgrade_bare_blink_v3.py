#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
裸机诊断固件 v3 —— 针对"LED 爆闪 1.5s 后停 + 串口无输出"的修正。

上一版两个设计缺陷：
  1) 延时函数可能被优化：`uint32_t i = 4000U; while (i--) { __NOP(); }` 无副作用，
     编译器可整体删除，导致"延时"瞬间返回 -> LED 表现为爆闪。
     本次改为 `volatile` 计数器 + 运行期上界（既不被删、也不被展开）。
  2) 串口发送是**死等** `while (TXE==0)`：若 USART1 未正确使能/接线不通，
     会永久阻塞，LED 也随之中止 —— 与你观察到的现象吻合。
     本次改为**超时退出**，并且 LED 心跳独立于串口，永不停止。

LED 心跳（不可能看错，一个周期 3 秒）：
      闪 1 下 -> 停 1s -> 闪 2 下 -> 停 1s -> 闪 3 下 -> 停 1s -> 循环
      每组前的长亮由"间隔 1 秒"区分；同时指示串口发送是否成功：
        每组结束后 LED 长亮 300ms  = 本组串口发送成功
        每组结束后不亮             = 串口发送超时（TXE 未置位）

用法: python ota/scripts/upgrade_bare_blink_v3.py [--apply]
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

MARK_IF = '#if (BL_BARE_BLINK != 0)'
MARK_ELSE = '#else  /* ---------------- 正常 Bootloader ---------------- */'

BODY = r'''#if (BL_BARE_BLINK != 0)

/* ============ 裸机诊断固件 v3 ============
 * 目标：在"什么都不确定"的情况下，用 LED 与串口回答三个问题——
 *   1. MCU 是否在执行代码            -> LED 心跳
 *   2. 时钟/延时是否正常             -> 心跳周期是否为 3 秒
 *   3. USART1 能否发出数据           -> 串口是否有输出 + LED 尾标
 * 全寄存器操作，不调用 HAL；不依赖 HSE（HSI 16MHz 直接作系统时钟）。
 * ======================================== */

static void Bare_LedInit(void)
{
  RCC->AHB2ENR |= (1UL << 4);                 /* GPIOEEN */
  (void)RCC->AHB2ENR;
  GPIOE->MODER   = (GPIOE->MODER & ~(3UL << 0)) | (1UL << 0);
  GPIOE->OTYPER &= ~(1UL << 0);
  GPIOE->OSPEEDR = (GPIOE->OSPEEDR & ~(3UL << 0)) | (3UL << 0);
  GPIOE->PUPDR   = (GPIOE->PUPDR & ~(3UL << 0));
}

static void Bare_LedOn(void)  { GPIOE->BRR  = (1UL << 0); }
static void Bare_LedOff(void) { GPIOE->BSRR = (1UL << 0); }

/* 延时 ms。
 * ⚠️ 计数器必须 volatile：否则 `while (i--) { __NOP(); }` 无副作用，
 *    编译器会把它整体删除，延时瞬间返回（表现为 LED 爆闪）。
 * ⚠️ 上界必须是运行期值（这里来自参数 ms）：若为编译期常量，会触发完全展开。 */
__attribute__((noinline))
static void Bare_DelayMs(uint32_t ms)
{
  while (ms-- != 0U)
  {
    volatile uint32_t i = 4000U;              /* HSI 16MHz 下约 1ms */
    while (i != 0U)
    {
      i--;
    }
  }
}

static void Bare_UartInit(void)
{
  /* USART1 内核时钟明确选 HSI16（= SYSCLK，避免 SystemInit 未配时钟的歧义） */
  RCC->CCIPR = (RCC->CCIPR & ~(3UL << 0)) | (2UL << 0);   /* USART1SEL = HSI16 */

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
  /* HSI16, OVER8=0 -> USARTDIV = 16e6/(16*115200) = 8.6806
   * BRR = (8 << 4) | round(0.6806*16) = 0x8B */
  USART1->BRR = 0x8BU;
  USART1->CR1 = (1UL << 3) | (1UL << 2) | (1UL << 0);   /* TE | RE | UE */
}

/* 发送字符串；返回 1 = 全部发出，0 = 超时（TXE 未置位）。
 * ⚠️ 绝不无限等待：否则串口有问题时整个程序会卡死，LED 也停。 */
static uint32_t Bare_Send(const char *s)
{
  while (*s != '\0')
  {
    uint32_t guard = 2000000U;
    while (((USART1->ISR & (1UL << 7)) == 0U) && (guard != 0U))
    {
      guard--;
    }
    if (guard == 0U)
    {
      return 0U;                              /* 超时：放弃本次发送 */
    }
    USART1->TDR = (uint32_t)(uint8_t)(*s);
    s++;
  }
  return 1U;
}

int main(void)
{
  static const char *const msgs[3] = {
    "BARE3: MCU alive, HSI 16MHz, no HSE used\r\n",
    "BARE3: USART1 115200 8N1 on PA9\r\n",
    "BARE3: led heartbeat = flash N times, 1s gap\r\n"
  };
  uint32_t round = 0U;
  uint32_t i;

  Bare_LedInit();
  Bare_UartInit();
  Bare_LedOff();

  for (;;)
  {
    round++;
    if (round > 3U)
    {
      round = 1U;
    }

    /* 闪 round 次（每次 120ms 亮 / 130ms 灭），随后停 1 秒 */
    for (i = 0U; i < round; i++)
    {
      Bare_LedOn();
      Bare_DelayMs(120U);
      Bare_LedOff();
      Bare_DelayMs(130U);
    }
    Bare_DelayMs(1000U);

    /* 每轮发一条消息；成功则该轮结束后长亮 300ms 作为"串口 OK"尾标 */
    if (Bare_Send(msgs[round - 1U]) != 0U)
    {
      Bare_LedOn();
      Bare_DelayMs(300U);
      Bare_LedOff();
    }
  }
}

'''

# 注意：本块**不含 #else**。替换时把原文的 MARK_ELSE 原样接在后面，
# 以保持 "#if ... #else ... #endif" 结构完整（否则预处理指令失衡）。


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()

    i_if = t.find(MARK_IF)
    i_else = t.find(MARK_ELSE, i_if + 1)
    if i_if < 0 or i_else < 0:
        print('  !! 未找到条件块（#if=%d, #else=%d）' % (i_if, i_else))
        return 2

    body = BODY.replace('\n', '\r\n') if '\r\n' in t else BODY
    # body 末尾保持 "#else" 上下文：把 MARK_ELSE 原文接在后面
    t2 = t[:i_if] + body + MARK_ELSE + t[i_else + len(MARK_ELSE):]

    print('  旧块 %d 字节 -> 新块 %d 字节' % (i_else - i_if, len(body)))
    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(MAIN, 'w', encoding='utf-8', newline='').write(t2)
    print('  已写入')
    return 0


if __name__ == '__main__':
    sys.exit(main())
