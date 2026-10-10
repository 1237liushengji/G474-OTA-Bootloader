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
   * 复位、串口停在某一行"，一条线索都留不下（实测踩到）。此处先把故障寄存器与
   * 故障指令地址打出来（直接写 USART1 寄存器，不依赖 HAL / 中断），再保持原语义
   * 停机等复位。判读：
   *   CFSR.bit1  DACCVIOL / bit0 IACCVIOL  -> 取指/取数违例（常见于调用空指针）
   *   CFSR.bit9  PRECISERR + BFAR 有效     -> 访问了非法/未使能的外设地址
   *   CFSR.bit10 IMPRECISERR               -> 写缓冲导致的异步总线错误
   *   CFSR.bit25 DIVBYZERO / bit24 UNALIGNED -> 除零/非对齐
   *   HFSR.bit1  VECTTBL                   -> 取向量失败（VTOR/向量表问题） */
  {
    uint32_t exc_return;
    uint32_t sp;
    uint32_t stacked_pc;

    __ASM volatile ("mov %0, lr" : "=r" (exc_return));
    sp = ((exc_return & 0x4U) != 0U) ? __get_PSP() : __get_MSP();
    stacked_pc = ((const volatile uint32_t *)sp)[6];   /* 栈帧第 7 字 = 故障 PC */

    BSP_UART_EmergencyPuts("\r\n!! HARDFAULT !!\r\n");
    BSP_UART_EmergencyHex("CFSR  = ", SCB->CFSR);
    BSP_UART_EmergencyHex("HFSR  = ", SCB->HFSR);
    BSP_UART_EmergencyHex("MMFAR = ", SCB->MMFAR);
    BSP_UART_EmergencyHex("BFAR  = ", SCB->BFAR);
    BSP_UART_EmergencyHex("PC    = ", stacked_pc);
    BSP_UART_EmergencyHex("MSP   = ", __get_MSP());
    BSP_UART_EmergencyHex("PSP   = ", __get_PSP());
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

/**
  * @brief FDCAN2中断线0服务函数（接收新报文/总线错误状态均路由到中断线0）
  */
void FDCAN2_IT0_IRQHandler(void)
{
  HAL_FDCAN_IRQHandler(&hfdcan2);
}

/**
  * @brief USART1中断服务函数（控制台串口收发）
  */
void USART1_IRQHandler(void)
{
  HAL_UART_IRQHandler(&huart1);
}

/* USER CODE END 1 */
