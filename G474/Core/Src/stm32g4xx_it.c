/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    stm32g4xx_it.c
  * @brief   Interrupt Service Routines.
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2024 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "stm32g4xx_it.h"
/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "can.h"
#include "bsp_uart.h"
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN TD */

/* USER CODE END TD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/* USER CODE END 0 */

/* External variables --------------------------------------------------------*/

/* USER CODE BEGIN EV */

/* USER CODE END EV */

/******************************************************************************/
/*           Cortex-M4 Processor Interruption and Exception Handlers          */
/******************************************************************************/
/**
  * @brief This function handles Non maskable interrupt.
  */
void NMI_Handler(void)
{
  /* USER CODE BEGIN NonMaskableInt_IRQn 0 */

  /* USER CODE END NonMaskableInt_IRQn 0 */
  /* USER CODE BEGIN NonMaskableInt_IRQn 1 */
   while (1)
  {
  }
  /* USER CODE END NonMaskableInt_IRQn 1 */
}

/**
  * @brief This function handles Hard fault interrupt.
  */
void HardFault_Handler(void)
{
  /* USER CODE BEGIN HardFault_IRQn 0 */

  /* 【2026-10-10 诊断】原实现是静默 `while(1)`，故障现场只剩"约 3 秒后被 IWDG
   * 复位、串口停在某一行"，一条线索都留不下（实测踩到）。
   * 顺序很重要：**先打印内核寄存器，再尝试读栈帧** —— 栈帧读取一旦因 sp 异常
   * 二次触发故障会直接进 Lockup，什么都打不出来。 */
  BSP_UART_FaultDump("HARDFAULT");
  {
    uint32_t exc_return;
    uint32_t sp;
    uint32_t stacked_pc;

    __ASM volatile ("mov %0, lr" : "=r" (exc_return));
    sp = ((exc_return & 0x4U) != 0U) ? __get_PSP() : __get_MSP();
    stacked_pc = ((const volatile uint32_t *)sp)[6];   /* 栈帧第 7 字 = 故障 PC */
    BSP_UART_EmergencyHex("  PC    = ", stacked_pc);
  }

  /* USER CODE END HardFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_HardFault_IRQn 0 */
    /* USER CODE END W1_HardFault_IRQn 0 */
  }
}

/**
  * @brief This function handles Memory management fault.
  */
void MemManage_Handler(void)
{
  /* USER CODE BEGIN MemoryManagement_IRQn 0 */
  /* 【2026-10-12】先把异常栈帧里的 fault PC 打出来 —— 这是唯一能定位到
   * "哪条指令出的错"的信息。
   * 背景：排查 USB CDC 时的 MemManage 故障，CFSR/HFSR 全为 0，光看它们
   * 完全无从下手；而栈帧里被硬件压入的 PC 是确定性的证据。
   * 顺序要点：**必须先取 sp 再调用任何函数**，否则栈会被下面的打印调用
   * 覆盖，帧就找不到了。 */
  {
    uint32_t exc_return;
    uint32_t sp;

    __ASM volatile ("mov %0, lr" : "=r" (exc_return));
    sp = ((exc_return & 0x4U) != 0U) ? __get_PSP() : __get_MSP();
    BSP_UART_PrintExcFrame(sp);
  }
  BSP_UART_FaultDump("MEMMANAGE FAULT");
  /* USER CODE END MemoryManagement_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_MemoryManagement_IRQn 0 */
    /* USER CODE END W1_MemoryManagement_IRQn 0 */
  }
}

/**
  * @brief This function handles Prefetch fault, memory access fault.
  */
void BusFault_Handler(void)
{
  /* USER CODE BEGIN BusFault_IRQn 0 */
  BSP_UART_FaultDump("BUSFAULT");
  /* USER CODE END BusFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_BusFault_IRQn 0 */
    /* USER CODE END W1_BusFault_IRQn 0 */
  }
}

/**
  * @brief This function handles Undefined instruction or illegal state.
  */
void UsageFault_Handler(void)
{
  /* USER CODE BEGIN UsageFault_IRQn 0 */
  BSP_UART_FaultDump("USAGEFAULT");
  /* USER CODE END UsageFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_UsageFault_IRQn 0 */
    /* USER CODE END W1_UsageFault_IRQn 0 */
  }
}

/**
  * @brief This function handles System service call via SWI instruction.
  */
void SVC_Handler(void)
{
  /* USER CODE BEGIN SVCall_IRQn 0 */

  /* USER CODE END SVCall_IRQn 0 */
  /* USER CODE BEGIN SVCall_IRQn 1 */

  /* USER CODE END SVCall_IRQn 1 */
}

/**
  * @brief This function handles Debug monitor.
  */
void DebugMon_Handler(void)
{
  /* USER CODE BEGIN DebugMonitor_IRQn 0 */

  /* USER CODE END DebugMonitor_IRQn 0 */
  /* USER CODE BEGIN DebugMonitor_IRQn 1 */

  /* USER CODE END DebugMonitor_IRQn 1 */
}

/**
  * @brief This function handles Pendable request for system service.
  */
void PendSV_Handler(void)
{
  /* USER CODE BEGIN PendSV_IRQn 0 */

  /* USER CODE END PendSV_IRQn 0 */
  /* USER CODE BEGIN PendSV_IRQn 1 */

  /* USER CODE END PendSV_IRQn 1 */
}

/**
  * @brief This function handles System tick timer.
  */
void SysTick_Handler(void)
{
  /* USER CODE BEGIN SysTick_IRQn 0 */
  /* 【2026-10-18 清理】原"主循环阶段 1ms 采样"已随 g_loopStage* 一起移除。
   * 它把每个任务入口写成全局变量并在 1ms 中断里累加 age，纯属排障脚手架，
   * 出厂版本不需要，也不应在 1ms 中断里做无意义的比较与写操作。 */
  /* USER CODE END SysTick_IRQn 0 */
  HAL_IncTick();
  /* USER CODE BEGIN SysTick_IRQn 1 */

  /* USER CODE END SysTick_IRQn 1 */
}

/******************************************************************************/
/* STM32G4xx Peripheral Interrupt Handlers                                    */
/* Add here the Interrupt Handlers for the used peripherals.                  */
/* For the available peripheral interrupt handler names,                      */
/* please refer to the startup file (startup_stm32g4xx.s).                    */
/******************************************************************************/

/* USER CODE BEGIN 1 */

/* 【2026-10-10 诊断】FDCAN2 中断线0 入口计数。
 * 用途：APP 启动卡在 CAN_SelfTest() 内（已由启动打点证实），需要区分
 *   "主代码死循环" 与 "中断风暴/卡在 ISR 里"。
 * 前 3 次 + 每 1000 次打印一次（限制刷屏），计数本身在主代码里也会被打印。 */
volatile uint32_t g_fdcanIsrCnt = 0U;

/**
  * @brief FDCAN2中断线0服务函数（接收新报文/总线错误状态均路由到中断线0）
  */
void FDCAN2_IT0_IRQHandler(void)
{
  g_fdcanIsrCnt++;

  /* ⚠️【2026-10-11 修复】此处**禁止**做任何串口输出。
   *
   * 原先写了 `BSP_UART_EmergencyHex(...)`（前 3 次 + 每 1000 次）：
   *   BSP_UART_EmergencyPuts 是"逐字符轮询直写 USART1->TDR"的阻塞式发送，
   *   每个字符最多空转 200000 次等 TXE。FDCAN2 中断优先级为 1（高于 SysTick），
   *   在中断上下文里做这种阻塞 IO 会：
   *     ① 把 ISR 时长从微秒级拉到毫秒级；
   *     ② 与主循环的 Console/BootMark 争用同一个 TDR，输出互相踩踏；
   *     ③ 中断频繁时主循环被整体饿死。
   *   计数 g_fdcanIsrCnt 仍保留，用 SWD 直接读即可，不需要打印。
   *
   * ⚠️【2026-10-12】本 ISR 已**整体停用**：hal_msp.c 改为
   *   HAL_NVIC_DisableIRQ(FDCAN2_IT0_IRQn)，CAN 收发全部走 CAN_Task 轮询。
   *   停用原因：板上 SWD 冻结态抓到 PC 停在弱桩 `E7FE` 死循环、IPSR=102(=IRQ86)、
   *   而本函数体一次都没执行过 → 首次服务落入弱桩别名后永不返回，
   *   高优先级 IRQ 饿死 SysTick → 3s IWDG 复位 → 无限循环。
   *   本函数保留仅为 ISR 路径兜底，**不要**重新 Enable 该 IRQ。
   */
  HAL_FDCAN_IRQHandler(&hfdcan2);
}

/**
  * @brief USART1中断服务函数（控制台串口收发）
  *
  * 【2026-10-11 诊断】在进 HAL 之前先记一笔"最原始"的入口次数，
  * 并把 ISR 快照存下来 —— 不依赖 HAL 的任何状态机判断。
  * 用途：区分下列三种情况（PC 正在发送时，用 SWD 读 g_uartRawIsrCnt / g_uartRawIsrSnap）：
  *   - g_uartRawIsrCnt == 0            -> USART1 中断从未进入
  *   - 进入但 ISR.RXNE 从未置位        -> 无数据到达（物理层）
  *   - 进入且 RXNE 置位                -> 数据到了，看 HAL 为何没回显
  */
volatile uint32_t g_uartRawIsrCnt  = 0U;   /* USART1 中断入口计数（最原始） */
volatile uint32_t g_uartRawIsrSnap = 0U;   /* 首次进入时的 ISR 快照 */
volatile uint32_t g_uartRawSrSnap  = 0U;   /* SR/ISR 在入口处的原始值 */

void USART1_IRQHandler(void)
{
  g_uartRawIsrCnt++;
  if (g_uartRawSrSnap == 0U)
  {
    g_uartRawIsrSnap = USART1->ISR;
    g_uartRawSrSnap  = USART1->ISR;
  }
  HAL_UART_IRQHandler(&huart1);
}

/* USER CODE END 1 */
