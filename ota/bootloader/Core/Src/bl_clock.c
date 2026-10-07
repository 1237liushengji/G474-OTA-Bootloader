/**
  ******************************************************************************
  * @file    bl_clock.c
  * @brief   SystemClock_Config（照抄既有 APP 工程取值）+ HAL_MspInit
  *
  * 时钟：HSE 8MHz -> PLL M=2, N=75, R=2 -> SYSCLK = 150MHz
  *       AHB=150MHz, APB1=150MHz, APB2=150MHz, FLASH_LATENCY_4
  *       FDCAN 内核时钟 = PCLK1 = 150MHz（500k 位时序按此计算）
  *
  * HAL_MspInit 从既有工程的 stm32g4xx_hal_msp.c 平移：使能 SYSCFG/PWR 时钟并
  * 关闭 UCPD 死电池引脚内部上拉。BL 是自包含工程，不引入 CubeMX 生成的 msp/it 文件，
  * 因此这里自带一份（HAL_MspInit 是 __weak，可安全覆盖）。
  ******************************************************************************
  */
#include "bl_clock.h"

/**
  * @brief  HAL 全局 MSP 初始化（HAL_Init 内部调用）
  */
void HAL_MspInit(void)
{
  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_RCC_PWR_CLK_ENABLE();

  /* 关闭 UCPD 死电池引脚的内部上拉（与既有工程一致） */
  HAL_PWREx_DisableUCPDDeadBattery();
}

/**
  * @brief 系统时钟配置：HSE 8M -> PLL(M=2,N=75,R=2) -> 150MHz
  */
/* ---- 实际使用的时钟源（供 main 在 UART 就绪后上报） ---- */
BL_ClkSource_t s_clkSource = BL_CLK_UNKNOWN;

BL_ClkSource_t BL_ClockSource(void)
{
  return s_clkSource;
}
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** 主内部稳压器输出电压等级 */
  HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE1);

  /** 振荡器：优先 HSE + PLL -> 150MHz；HSE 起振失败则回落 HSI + PLL。
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
    RCC_OscInitStruct.PLL.PLLN       = 75;              /* x75 = 300MHz VCO（96~344 合法）*/
    RCC_OscInitStruct.PLL.PLLP       = RCC_PLLP_DIV2;
    RCC_OscInitStruct.PLL.PLLQ       = RCC_PLLQ_DIV2;
    RCC_OscInitStruct.PLL.PLLR       = RCC_PLLR_DIV2;   /* /2 = 150MHz，与 HSE 路径同频 */
    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
    {
      Error_Handler();                                  /* HSI 也失败：真的没救了 */
    }
  }
  else
  {
    s_clkSource = BL_CLK_HSE;
  }

  /** CPU/AHB/APB 总线时钟 */
  RCC_ClkInitStruct.ClockType      = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK |
                                     RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource   = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider  = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_4) != HAL_OK)
  {
    Error_Handler();
  }
}
