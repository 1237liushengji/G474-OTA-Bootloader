/**
  ******************************************************************************
  * @file    bl_jump.c
  * @brief   跳转到 APP 实现（BL -> APP 的交接）
  *
  * 交接顺序（每一步都有理由）：
  *   1) 关中断：清 PRIMASK，保证下面几步原子；
 *   2) 停 SysTick、清 NVIC 使能与挂起位、再清 SCB->ICSR 的系统异常挂起位：
 *      否则 APP 还没初始化自己的中断，BL 遗留的挂起中断会按 **BL 向量表** 取向量执行
 *      （APP 尚未接管）；
  *   3) 释放 BL 占用的外设（FDCAN / USART）：让 APP 的初始化从干净状态开始；
  *   4) SCB->VTOR = APP 向量表地址（APP 的 IROM 起始 = appBase）；
  *   5) __set_MSP(APP 初始 SP)，DSB/ISB 保证写入生效；
  *   6) 开中断后再跳转 —— 必须开！BL 里关掉 PRIMASK 后 APP 不是复位进入的，
  *      PRIMASK 不会被硬件自动清零，若不开中断 APP 的 SysTick/串口中断全都不会来；
 *   7) 跳到 APP 复位向量（bit0=1 表示 Thumb，已在 BL_ImageValidate 校验过）。
 *
 * ================================ 【H1 修复说明】 ================================
 * 检查报告指出两处问题，此处逐条给结论，避免后人误改回去：
 *
 * (1) 第 2 步只清 NVIC 不够 —— **真缺陷，已修**
 *     Cortex-M4 的挂起位分两处，NVIC->ICPR 只管得着"外设中断"（IRQ 0..239）：
 *       - 外设 IRQ : NVIC->ICPR[i]
 *       - 系统异常  : **只能**通过 SCB->ICSR 清，NVIC 对它们完全无效
 *     本工程 SysTick 每 1ms 挂起一次（BL 主循环时基），而下面那 8 次循环根本没碰它。
 *     改 VTOR 之后若 SysTick 仍 pending，APP 入口后会在其生效的那一瞬按
 *     **APP 向量表** 取向量 —— 但此刻 APP 还没走完初始化，表现为"APP 随机 HardFault /
 *     偶发启动即死"，且极难复现（只在跳转瞬间恰处 SysTick 窗口内，约千分之一量级）。
 *     已加入：SCB->ICSR = PENDSTCLR(bit25) | PENDSVCLR(bit27)（写 1 清除，不是置位）。
 *
 * (2) "__enable_irq() 在 entry() 之前有窗口" —— **有意为之且必要，不改**
 *     PRIMASK 只有复位才会被硬件清零，APP 不是复位进入的。若带着 PRIMASK=1 跳过去，
 *     APP 永远收不到中断（SysTick/UART/FDCAN 全哑），后果比"窗口期误触发"严重得多。
 *     窗口期误触发的概率已由 (1) 的清挂起位降到可忽略：能触发中断的前提是新 pending
 *     被重新置位，而此刻 SysTick 已停、外设已 DeInit、VTOR 已指向 APP。
 *     保留"开中断 -> DSB/ISB -> entry()"三步不动。
 * ==========================================================================
 ******************************************************************************
 */
#include "bl_jump.h"
#include "bl_can.h"
#include "bl_uart.h"

/*--------------------------------------- 内部类型 ----------------------------------------*/
typedef void (*BL_AppEntry)(void);                 /* APP 复位入口（无参无返回） */

/**
  * @brief  跳转到 APP（不返回）
  */
void BL_JumpToApp(uint32_t appBase)
{
  uint32_t    sp;
  uint32_t    rv;
  BL_AppEntry entry;
  uint32_t    i;

  /* 按 Flash 实际内容取 APP 向量表前两项（不使用可能过期的 RAM 缓存） */
  sp    = *(volatile const uint32_t *)appBase;
  rv    = *(volatile const uint32_t *)(appBase + 4U);
  entry = (BL_AppEntry)rv;

  __disable_irq();

  /* 2) 停 SysTick + 清 NVIC */
  SysTick->CTRL = 0U;
  SysTick->LOAD = 0U;
  SysTick->VAL  = 0U;

  for (i = 0U; i < 8U; i++)
  {
    NVIC->ICER[i] = 0xFFFFFFFFU;                   /* 关所有外设中断 */
    NVIC->ICPR[i] = 0xFFFFFFFFU;                   /* 清所有挂起位 */
  }

  /* 【H1 修复】清系统异常挂起位 —— NVIC->ICPR 对 SysTick/PendSV 无效，必须走 SCB->ICSR。
   * SCB->ICSR 是"写 1 清除"语义：
   *   bit25 PENDSTCLR  = 清 SysTick 挂起（本工程 1ms 周期中断，最容易残留）
   *   bit27 PENDSVCLR  = 清 PendSV 挂起（BL 没用，但一并清更干净）
   * 不清的后果：VTOR 已切到 APP 后，残留的 SysTick pending 会让 APP 在初始化完成前
   * 就按自己的向量表取向量执行 -> 随机 HardFault，且约千分之一概率才复现一次。*/
  SCB->ICSR = SCB_ICSR_PENDSTCLR_Msk | SCB_ICSR_PENDSVCLR_Msk;

  /* 3) 释放 BL 占用的外设（APP 会重新初始化） */
  BL_CanDeInit();
  BL_UartDeInit();

  /* 4) 向量表切到 APP */
  SCB->VTOR = appBase;

  /* 5) 设置 APP 栈顶 */
  __set_MSP(sp);
  __DSB();
  __ISB();

  /* 6) 恢复中断使能（APP 不是复位进入，PRIMASK 不会被硬件清零） */
  __enable_irq();
  __DSB();
  __ISB();

  /* 7) 跳转 */
  entry();

  /* 正常不返回；异常情况下停在此处（保持可被调试器/看门狗接管） */
  for (;;)
  {
  }
}
