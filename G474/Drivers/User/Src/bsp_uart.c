/**
  ******************************************************************************
  * @file    bsp_uart.c
  * @brief   控制台串口驱动实现
  *
  * 设计说明：
  *   1. 发送：阻塞发送（控制台为低速率人机交互，单行<100字节@115200约10ms，
  *      对1Hz的CAN业务无影响；若未来扩展高负载日志，再升级为中断/DMA发送）；
  *   2. 接收：单字节中断接收 + 128字节环形缓冲（单生产者中断/单消费者主循环，
  *      无锁安全），控制台任务轮询取走；
  *   3. 缓冲满时丢弃新字节并统计（保底不阻塞中断）。
  ******************************************************************************
  */
#include "bsp_uart.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

UART_HandleTypeDef huart1;

/*--------------------------------------- 模块内部变量 --------------------------------------*/
static uint8_t  s_rxRing[BSP_CONSOLE_RX_RING_SIZE];   /* 接收环形缓冲 */
static volatile uint16_t s_rxHead;                    /* 写指针（仅中断修改） */
static volatile uint16_t s_rxTail;                    /* 读指针（仅主循环修改） */
static uint8_t  s_rxBuf;                              /* 单字节中断接收缓冲 */

/* 【2026-10-09 真机诊断】接收链路诊断计数器。上位机 TUN ON 收不到应答，
 * 而"设备能发、PC 发什么都不回"说明是**接收**方向断。二者必有一个坏：
 *   - s_rxIrqCnt==0  -> UART 中断根本没进（NVIC/USART 未就绪，或 PA10 接线/复用有问题）
 *   - s_rxIrqCnt>0  -> 中断在跑，问题在别处（行解析/命令匹配）
 * 挂在 stats 命令里打印，纯诊断、不影响功能，验证完可保留。 */
static volatile uint32_t s_rxIrqCnt;                  /* UART Rx 中断触发次数 */
static volatile uint32_t s_rxDropCnt;                 /* 环形缓冲满被丢弃的字节数 */

/**
  * @brief  取 UART 接收中断触发次数（诊断用，见 Cmd_Stats）
  */
uint32_t BSP_UART_RxIrqCount(void)
{
  return s_rxIrqCnt;
}

/**
  * @brief  取因环形缓冲满而被丢弃的字节数（诊断用）
  */
uint32_t BSP_UART_RxDropCount(void)
{
  return s_rxDropCnt;
}

/**
  * @brief  初始化控制台串口：时钟、GPIO复用、USART1、中断、启动接收
  */
void BSP_UART_Init(void)
{
  GPIO_InitTypeDef gpioInit = {0};

  __HAL_RCC_GPIOA_CLK_ENABLE();
  BSP_CONSOLE_CLK_ENABLE();

  /* TX推挽复用；RX上拉复用（串口空闲态为高） */
  gpioInit.Pin       = BSP_CONSOLE_TX_PIN;
  gpioInit.Mode      = GPIO_MODE_AF_PP;
  gpioInit.Pull      = GPIO_NOPULL;
  gpioInit.Speed     = GPIO_SPEED_FREQ_HIGH;
  gpioInit.Alternate = BSP_CONSOLE_GPIO_AF;
  HAL_GPIO_Init(BSP_CONSOLE_TX_PORT, &gpioInit);

  gpioInit.Pin       = BSP_CONSOLE_RX_PIN;
  gpioInit.Pull      = GPIO_PULLUP;
  HAL_GPIO_Init(BSP_CONSOLE_RX_PORT, &gpioInit);

  /* 8N1，无流控 */
  huart1.Instance          = BSP_CONSOLE_USART;
  huart1.Init.BaudRate     = BSP_CONSOLE_BAUDRATE;
  huart1.Init.WordLength   = UART_WORDLENGTH_8B;
  huart1.Init.StopBits     = UART_STOPBITS_1;
  huart1.Init.Parity       = UART_PARITY_NONE;
  huart1.Init.Mode         = UART_MODE_TX_RX;
  huart1.Init.HwFlowCtl    = UART_HWCONTROL_NONE;
  huart1.Init.OverSampling = UART_OVERSAMPLING_16;
  if (HAL_UART_Init(&huart1) != HAL_OK)
  {
    Error_Handler();
  }

  /* 优先级低于FDCAN中断（1），串口交互实时性要求低 */
  HAL_NVIC_SetPriority(BSP_CONSOLE_USART_IRQn, 3, 0);
  HAL_NVIC_EnableIRQ(BSP_CONSOLE_USART_IRQn);

  (void)HAL_UART_Receive_IT(&huart1, &s_rxBuf, 1U);   /* 挂起首次单字节接收 */
}

/**
  * @brief  阻塞发送一段数据（超时保护100ms）
  */
void BSP_UART_Send(const char *data, uint16_t len)
{
  if ((data != NULL) && (len != 0U))
  {
    (void)HAL_UART_Transmit(&huart1, (const uint8_t *)data, len, 100U);
  }
}

/**
  * @brief  格式化打印（行缓冲96字节，够单行报文/统计输出）
  */
void BSP_UART_Printf(const char *fmt, ...)
{
  static char txBuf[96];                              /* 仅主循环调用，无重入 */
  va_list     args;
  int         n;

  va_start(args, fmt);
  n = vsnprintf(txBuf, sizeof(txBuf), fmt, args);
  va_end(args);

  if (n > 0)
  {
    if (n > (int)sizeof(txBuf))
    {
      n = (int)sizeof(txBuf);                         /* 截断保护 */
    }
    BSP_UART_Send(txBuf, (uint16_t)n);
  }
}

/**
  * @brief  当前可读字节数
  */
uint16_t BSP_UART_Available(void)
{
  uint16_t head = s_rxHead;
  uint16_t tail = s_rxTail;

  return (uint16_t)((head - tail + BSP_CONSOLE_RX_RING_SIZE) % BSP_CONSOLE_RX_RING_SIZE);
}

/**
  * @brief  取走一个字节
  * @retval 字节值；-1表示缓冲为空
  */
int16_t BSP_UART_GetChar(void)
{
  uint16_t head = s_rxHead;

  if (s_rxTail == head)
  {
    return -1;
  }

  {
    uint8_t c = s_rxRing[s_rxTail];
    s_rxTail  = (uint16_t)((s_rxTail + 1U) % BSP_CONSOLE_RX_RING_SIZE);
    return (int16_t)c;
  }
}

/**
  * @brief  UART接收完成回调（中断上下文）：写入环形缓冲并重新挂接收
  */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance == BSP_CONSOLE_USART)
  {
    uint16_t next = (uint16_t)((s_rxHead + 1U) % BSP_CONSOLE_RX_RING_SIZE);

    if (next != s_rxTail)
    {
      s_rxRing[s_rxHead] = s_rxBuf;
      s_rxHead           = next;
    }
    else
    {
      s_rxDropCnt++;                                 /* 缓冲满，丢弃 */
    }
    /* 缓冲满则丢弃本字节（控制台场景可接受，键入方会看到无回显） */
    s_rxIrqCnt++;

    (void)HAL_UART_Receive_IT(huart, &s_rxBuf, 1U);   /* 继续接收 */
  }
}
