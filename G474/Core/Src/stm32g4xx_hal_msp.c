/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file         stm32g4xx_hal_msp.c
  * @brief        This file provides code for the MSP Initialization
  *               and de-Initialization codes.
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
/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN TD */

/* USER CODE END TD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN Define */

/* USER CODE END Define */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN Macro */

/* USER CODE END Macro */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* External functions --------------------------------------------------------*/
/* USER CODE BEGIN ExternalFunctions */

/* USER CODE END ExternalFunctions */

/* USER CODE BEGIN 0 */

/* USER CODE END 0 */
/**
  * Initializes the Global MSP.
  */
void HAL_MspInit(void)
{

  /* USER CODE BEGIN MspInit 0 */

  /* USER CODE END MspInit 0 */

  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_RCC_PWR_CLK_ENABLE();

  /* System interrupt init*/

  /** Disable the internal Pull-Up in Dead Battery pins of UCPD peripheral
  */
  HAL_PWREx_DisableUCPDDeadBattery();

  /* USER CODE BEGIN MspInit 1 */

  /* USER CODE END MspInit 1 */
}

/* USER CODE BEGIN 1 */

/**
  * @brief FDCAN2底层初始化：内核时钟、GPIO复用、中断优先级
  *        （调用模式与ST官方G474E-EVAL FDCAN示例一致）
  * @param fdcanHandle FDCAN句柄
  */
void HAL_FDCAN_MspInit(FDCAN_HandleTypeDef* fdcanHandle)
{
  GPIO_InitTypeDef         GPIO_InitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInit   = {0};

  if (fdcanHandle->Instance == FDCAN2)
  {
    /* FDCAN 内核时钟使用 **HSE（8MHz）**。
     *
     * ⚠️【2026-10-11 更正】下面这段"内核时钟超频 / 写 TXBAR 挂死 AHB"的说法
     * **已被证伪**，是本项目最长寿的一条误诊结论，保留原文仅供追溯。
     *
     * 真实根因（见 docs/13 §2.5 与 §十）：提交 b2133f1 给 HAL 加探针时
     * **误删了 HAL_FDCAN_Init() 内约 90 行核心配置**，其中包含
     * **`Instance->NBTP = ...`（标称位时序寄存器的写入）** → NBTP 恒为 0
     * → 位时间非法 → 协议引擎无法发送 → IWDG 3 秒复位。
     *
     * 之所以曾误判成"内核时钟超频"，是因为当时读到的 TXBAR 挂死现象，
     * 前提是那份被掏空的 HAL —— 前提本身不成立。
     * HAL 逐字节还原官方基线 0cd7827 后，上板实测：
     *   nbtp=0x02000A03（位时序已写入）、init_status=0(HAL_OK)、
     *   CAN_Init() 仅耗时 4ms、psr LEC=7 无错 → **P1 已修复并经硬件验证**。
     *
     * 选择 HSE 作内核时钟本身**是合理的**（晶振精度优于 PLL 派生时钟，
     * 利于 CAN 位同步），只是它**不是**当初挂死的根因。
     * 位时序按 8MHz 重算为 **16tq/位、采样点 75%**（见 can.c 的 s_bitrateTable）。 */
    PeriphClkInit.PeriphClockSelection = RCC_PERIPHCLK_FDCAN;
    PeriphClkInit.FdcanClockSelection  = RCC_FDCANCLKSOURCE_HSE;
    if (HAL_RCCEx_PeriphCLKConfig(&PeriphClkInit) != HAL_OK)
    {
      Error_Handler();
    }

    /* FDCAN2与时钟 */
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_FDCAN_CLK_ENABLE();

    /* PB12 = FDCAN2_RX -> 收发器CTX/TXD；PB13 = FDCAN2_TX -> 收发器CRX/RXD
     * （板卡丝印：CAN_RX->PB12、CAN_TX->PB13；G474数据手册Table 13：AF9） */
    GPIO_InitStruct.Pin       = GPIO_PIN_12 | GPIO_PIN_13;
    GPIO_InitStruct.Mode      = GPIO_MODE_AF_PP;
    GPIO_InitStruct.Pull      = GPIO_NOPULL;
    GPIO_InitStruct.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    GPIO_InitStruct.Alternate = GPIO_AF9_FDCAN2;
    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

    /* 【2026-10-12 关键修复】显式禁用 FDCAN2 中断线 0 的 NVIC 使能。
     *
     * 故障：APP 上电后 3~5s 被 IWDG 复位，串口心跳停在 t=5233 / n=348276。
     * 板上 SWD 冻结态实测：PC=0x0800C3BE（`E7FE`=`b .` 弱桩死循环）、
     * XPSR IPSR=102（=IRQ86=FDCAN2_IT0）、NVIC IABR[2]/ISER[2] bit22 均置位、
     * 而 g_fdcanIsrCnt==0（ISR 函数体一次都没跑）→ 首次进入即落到弱桩别名，
     * 永不返回；该 IRQ 优先级 1 高于 SysTick 15 → 全系统冻结 → 3s IWDG 复位 → 死循环。
     *
     * 板上二分实验验证：仅写 NVIC ICER[2]=0（禁用 IRQ86）后放行内核，
     * 心跳一路跑到 t=5234 / n=348291，主循环稳定 16k 次/秒，40s 内零复位。
     *
     * 处置：CAN 收发全部改为 CAN_Task 主循环轮询（见 can.c），
     *       此处必须 Disable 而非 Enable，否则旧路径会再次被触发。 */
    HAL_NVIC_DisableIRQ(FDCAN2_IT0_IRQn);
    HAL_NVIC_DisableIRQ(FDCAN2_IT1_IRQn);
  }
}

/**
  * @brief FDCAN2底层反初始化
  * @param fdcanHandle FDCAN句柄
  */
void HAL_FDCAN_MspDeInit(FDCAN_HandleTypeDef* fdcanHandle)
{
  if (fdcanHandle->Instance == FDCAN2)
  {
    __HAL_RCC_FDCAN_CLK_DISABLE();

    HAL_GPIO_DeInit(GPIOB, GPIO_PIN_12 | GPIO_PIN_13);

    HAL_NVIC_DisableIRQ(FDCAN2_IT0_IRQn);
  }
}

/* USER CODE END 1 */
