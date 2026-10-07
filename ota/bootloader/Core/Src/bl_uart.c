/**
  ******************************************************************************
  * @file    bl_uart.c
  * @brief   BL 极简阻塞串口日志实现（USART1 PA9/PA10 115200 8N1）
  *
  * 设计取舍：
  *   - BL 只需要单向日志，**只做阻塞发送、不开接收中断**（比既有控制台驱动更简）；
  *     单行 <100 字节 @115200 约 10ms，对 OTA 时序无实质影响；
  *   - 所有 HAL 返回值都检查，失败计数（s_txErrors）不静默吞掉；
  *   - 句柄为本模块 static，不污染全局命名空间（BL 与 APP 各自独立工程）。
  ******************************************************************************
  */
#include "bl_uart.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/*--------------------------------------- 模块内部宏 --------------------------------------*/
#define BL_UART_INSTANCE     USART1
#define BL_UART_CLK_ENABLE   __HAL_RCC_USART1_CLK_ENABLE
#define BL_UART_TX_PORT      GPIOA
#define BL_UART_TX_PIN       GPIO_PIN_9       /* PA9  = USART1_TX */
#define BL_UART_RX_PORT      GPIOA
#define BL_UART_RX_PIN       GPIO_PIN_10      /* PA10 = USART1_RX */
#define BL_UART_GPIO_AF      GPIO_AF7_USART1
#define BL_UART_BAUDRATE     115200U
#define BL_UART_TX_TIMEOUT   100U             /* 阻塞发送超时（ms） */
#define BL_UART_LINE_BUF     96U              /* 格式化行缓冲 */

/*--------------------------------------- 模块内部变量 ------------------------------------*/
static UART_HandleTypeDef s_huart1;
static uint32_t           s_txErrors;         /* 发送失败次数（调试器可观察，不静默） */

/**
  * @brief  初始化 USART1（PA9/PA10，AF7，115200 8N1）
  */
void BL_UartInit(void)
{
  GPIO_InitTypeDef gpioInit = {0};

  __HAL_RCC_GPIOA_CLK_ENABLE();
  BL_UART_CLK_ENABLE();

  /* TX 推挽复用；RX 上拉复用（串口空闲态为高） */
  gpioInit.Pin       = BL_UART_TX_PIN;
  gpioInit.Mode      = GPIO_MODE_AF_PP;
  gpioInit.Pull      = GPIO_NOPULL;
  gpioInit.Speed     = GPIO_SPEED_FREQ_HIGH;
  gpioInit.Alternate = BL_UART_GPIO_AF;
  HAL_GPIO_Init(BL_UART_TX_PORT, &gpioInit);

  gpioInit.Pin       = BL_UART_RX_PIN;
  gpioInit.Pull      = GPIO_PULLUP;
  HAL_GPIO_Init(BL_UART_RX_PORT, &gpioInit);

  /* ⚠️ 必须先把整个句柄清零再逐字段赋值。
   * 原因（实测踩过）：`UART_InitTypeDef` 里**必须设**的字段不止下面这几个，
   * 还有 `ClockPrescaler` / `OneBitSampling` / `AdvFeatureInit` 等。
   * HAL 会用 `UART_DIV_SAMPLING16(pclk, baud, huart->Init.ClockPrescaler)`
   * 去查 `UARTPrescTable[ClockPrescaler]` —— 该字段没设就是**随机索引**，
   * 越界后总线访问落到非法地址（实测 BFAR=0x20FB0EA4）→ 精确总线错误 → HardFault
   * （PC 停在 HardFault_Handler 0x08000936，表现为"上电没反应"）。
   *
   * 这个坑能出现，是因为散列文件曾把 RAM 域标成 `UNINIT`（启动不清 .bss）。
   * 那个 `UNINIT` 已在 ota/scripts/bl_layout.py 里去掉；这里再显式清零，
   * 两道一起上 —— 少一道都会在某些上电时序下变成"看运气"。 */
  (void)memset(&s_huart1, 0, sizeof(s_huart1));

  s_huart1.Instance          = BL_UART_INSTANCE;
  s_huart1.Init.BaudRate     = BL_UART_BAUDRATE;
  s_huart1.Init.WordLength   = UART_WORDLENGTH_8B;
  s_huart1.Init.StopBits     = UART_STOPBITS_1;
  s_huart1.Init.Parity       = UART_PARITY_NONE;
  s_huart1.Init.Mode         = UART_MODE_TX_RX;
  s_huart1.Init.HwFlowCtl    = UART_HWCONTROL_NONE;
  s_huart1.Init.OverSampling = UART_OVERSAMPLING_16;
  s_huart1.Init.ClockPrescaler = UART_PRESCALER_DIV1;   /* 必须显式设：HAL 拿它当查表下标 */
  if (HAL_UART_Init(&s_huart1) != HAL_OK)
  {
    Error_Handler();                              /* 串口是 BL 唯一的现场诊断通道 */
  }
}

/**
  * @brief  阻塞发送一段数据
  */
void BL_UartSend(const char *data, uint16_t len)
{
  if ((data == NULL) || (len == 0U))
  {
    return;
  }

  if (HAL_UART_Transmit(&s_huart1, (const uint8_t *)data, len, BL_UART_TX_TIMEOUT) != HAL_OK)
  {
    s_txErrors++;                                 /* 不静默：计数供调试观察 */
  }
}

/**
  * @brief  格式化打印（静态行缓冲，仅主循环调用）
  */
void BL_UartPrintf(const char *fmt, ...)
{
  static char txBuf[BL_UART_LINE_BUF];
  va_list     args;
  int         n;

  if (fmt == NULL)
  {
    return;
  }

  va_start(args, fmt);
  n = vsnprintf(txBuf, sizeof(txBuf), fmt, args);
  va_end(args);

  if (n > 0)
  {
    if (n > (int)(sizeof(txBuf) - 1U))
    {
      n = (int)(sizeof(txBuf) - 1U);              /* 截断保护（vsnprintf 已保证结尾 0） */
    }
    BL_UartSend(txBuf, (uint16_t)n);
  }
}

/**
  * @brief  释放 USART1（跳转 APP 前调用）
  */
void BL_UartDeInit(void)
{
  (void)HAL_UART_DeInit(&s_huart1);
  HAL_GPIO_DeInit(BL_UART_TX_PORT, BL_UART_TX_PIN);
  HAL_GPIO_DeInit(BL_UART_RX_PORT, BL_UART_RX_PIN);
}

/* ===================== HAL UART 扩展弱回调（空实现） =====================
 * 本 BL 只做阻塞收发，不使用 UART 的 RxFifoFull / TxFifoEmpty / Wakeup 扩展功能。
 * 这些回调原本由 stm32g4xx_hal_uart_ex.c 以 __weak 提供；为减小 BL 体积，
 * 该文件未被编入 BL 工程，因此在此提供空实现满足链接。
 * 注意：HAL_UART_DeInit() 会调用 HAL_DMA_Abort()（在 stm32g4xx_hal_dma.c 中），
 *       故 DMA 模块必须保留在工程里。
 * ====================================================================== */
void HAL_UARTEx_RxFifoFullCallback(UART_HandleTypeDef *huart)
{
  UNUSED(huart);
}

void HAL_UARTEx_TxFifoEmptyCallback(UART_HandleTypeDef *huart)
{
  UNUSED(huart);
}

void HAL_UARTEx_WakeupCallback(UART_HandleTypeDef *huart)
{
  UNUSED(huart);
}
