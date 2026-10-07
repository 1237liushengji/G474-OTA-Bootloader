/**
  ******************************************************************************
  * @file    bl_clock.h
  * @brief   BL 时钟配置与 HAL MSP
  ******************************************************************************
  */
#ifndef __BL_CLOCK_H
#define __BL_CLOCK_H

#include "bl_main.h"

/**
  * @brief  系统时钟：HSE 8MHz -> PLL(M=2,N=75,R=2) -> SYSCLK/HCLK/PCLK1 = 150MHz
  *         （与既有 APP 工程 main.c 完全一致，FLASH_LATENCY_4）
  * @note   失败直接 Error_Handler（时钟是后续一切外设的前提）
  */
void SystemClock_Config(void);

/** 系统时钟来源（诊断用：区分"晶振正常"与"已回落 HSI"） */
typedef enum
{
  BL_CLK_UNKNOWN      = 0,
  BL_CLK_HSE          = 1,   /* HSE 8MHz + PLL -> 150MHz（正常） */
  BL_CLK_HSI_FALLBACK = 2    /* HSE 起振失败，已回落 HSI + PLL */
} BL_ClkSource_t;

BL_ClkSource_t BL_ClockSource(void);

#endif /* __BL_CLOCK_H */
