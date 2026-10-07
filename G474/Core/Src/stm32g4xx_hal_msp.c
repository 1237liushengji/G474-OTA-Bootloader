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
    /* FDCAN内核时钟选择PCLK1 = 150MHz（位时序参数按此时钟计算，见can.h） */
    PeriphClkInit.PeriphClockSelection = RCC_PERIPHCLK_FDCAN;
    PeriphClkInit.FdcanClockSelection  = RCC_FDCANCLKSOURCE_PCLK1;
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

    /* FDCAN2中断线0（仅次于内核异常的较高优先级，高于SysTick） */
    HAL_NVIC_SetPriority(FDCAN2_IT0_IRQn, 1, 0);
    HAL_NVIC_EnableIRQ(FDCAN2_IT0_IRQn);
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
