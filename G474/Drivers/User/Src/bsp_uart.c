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
  * @brief  接收链路自诊断：RX 完全收不到字节时，周期性把 USART1/GPIO/NVIC 的
  *         真实寄存器状态打出来（走已知可用的 TX 通路），用于区分
  *         "信号没到引脚" 与 "信号到了但 USART/中断没处理"。
  *
  * 【2026-10-10 新增】起因：真机上 TX 正常（banner 能打出），但 PC 发任何字符
  * 都零响应，连字符回显都没有。静态代码逐行核对全部正确（引脚 PA9/PA10+AF7、
  * huart1 单一定义、ISR 接线、NVIC 使能），所以必须靠**运行态测量**定位：
  *
  *   判据（PC 侧持续发送时看本 dump）：
  *     a) PA10 edges/20ms > 0，但 rxIrq 恒 0   -> 信号到了引脚，USART/中断配置问题
  *     b) PA10 edges/20ms == 0                 -> 信号根本没到引脚（接线/适配器问题）
  *     c) MODER 的 PA9/PA10 字段不是 0b10      -> 引脚被别处改写成非复用模式
  *     d) CR1 缺 RE 或 RXNEIE                  -> Receive_IT 未生效
  *     e) ISER1 bit5 == 0                      -> USART1_IRQn 未使能
  *
  * 仅在 s_rxIrqCnt==0（从未收到过）时打印，最多 30 次（约 90 秒），避免刷屏。
  */
/**
  * @brief  应急输出：直接写 USART1 寄存器发送一行文本。
  *
  * 【2026-10-10 新增】用于 HardFault_Handler / Error_Handler / 启动打点这类
  * "HAL 可能已不可用"的场景：不经过 HAL_UART_Transmit（它先检查 gState，
  * 状态不对会直接返回 HAL_BUSY 而什么都不发），也不依赖任何中断。
  *
  * 安全护栏：先检查 CR1 的 UE|TE 位；UART 尚未初始化时**直接放弃输出**，
  * 避免在"等 TXE"上死等（启动早期故障会走到这里）。
  */
void BSP_UART_EmergencyPuts(const char *s)
{
  if (s == NULL)
  {
    return;
  }
  /* UE = CR1.bit0，TE = CR1.bit3；未使能则无法发送，直接返回 */
  if ((USART1->CR1 & 0x00000009U) != 0x00000009U)
  {
    return;
  }

  while (*s != '\0')
  {
    while ((USART1->ISR & 0x00000080U) == 0U)   /* TXE：TDR 可写入 */
    {
    }
    USART1->TDR = (uint32_t)(uint8_t)(*s);
    s++;
  }
  while ((USART1->ISR & 0x00000040U) == 0U)     /* TC：最后一字节移出完毕 */
  {
  }
}

/**
  * @brief  应急输出一个 32 位十六进制值（形如 "TAG=XXXXXXXX\r\n"）
  */
void BSP_UART_EmergencyHex(const char *tag, uint32_t value)
{
  static const char kHexDigits[] = "0123456789ABCDEF";
  char              buf[9];
  int               i;

  for (i = 0; i < 8; i++)
  {
    buf[i] = kHexDigits[(value >> ((7 - i) * 4)) & 0x0FU];
  }
  buf[8] = '\0';

  BSP_UART_EmergencyPuts(tag);
  BSP_UART_EmergencyPuts(buf);
  BSP_UART_EmergencyPuts("\r\n");
}

/**
  * @brief  启动检查点：打印标记 + 当前 tick（十六进制）
  *
  * 【2026-10-10 新增】真机现象：APP 打印到 "main: led-bsrr-state M3" 后即被
  * IWDG 每 3 秒复位，主循环从未跑起来（因此串口 RX 看起来"完全无响应"——
  * Console_Task 根本没执行）。用这些检查点把卡点逐步缩小到一个函数内，
  * 同时用 tick 值判断 SysTick 是否在推进（tick 冻结会让 HAL 里所有
  * `while ((HAL_GetTick()-start) > TIMEOUT)` 变成死循环）。
  */
void BSP_UART_BootMark(const char *msg)
{
  static const char kHexDigits[] = "0123456789ABCDEF";
  uint32_t          t = HAL_GetTick();
  char              buf[9];
  int               i;

  for (i = 0; i < 8; i++)
  {
    buf[i] = kHexDigits[(t >> ((7 - i) * 4)) & 0x0FU];
  }
  buf[8] = '\0';

  BSP_UART_EmergencyPuts("[boot] ");
  BSP_UART_EmergencyPuts(msg);
  BSP_UART_EmergencyPuts(" tick=0x");
  BSP_UART_EmergencyPuts(buf);
  BSP_UART_EmergencyPuts("\r\n");
}

void BSP_UART_DiagTask(void)
{
  static uint32_t s_next = 0U;
  static uint32_t s_cnt  = 0U;
  uint32_t        now;
  uint32_t        edges = 0U;
  uint32_t        prev;
  uint32_t        t0;

  if ((s_rxIrqCnt != 0U) || (s_cnt >= 30U))
  {
    return;
  }

  now = HAL_GetTick();
  if ((s_next != 0U) && ((int32_t)(now - s_next) < 0))
  {
    return;
  }
  s_next = now + 3000U;
  s_cnt++;

  /* 20ms 窗口内数 PA10 的跳变次数：PC 正在发数据时应有大量跳变。
   * 用忙等采样（非阻塞式不可得，UART 无变化中断），20ms 远小于 IWDG 的 3s。 */
  prev = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;
  t0   = HAL_GetTick();
  while ((HAL_GetTick() - t0) < 20U)
  {
    uint32_t cur = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;
    if (cur != prev)
    {
      edges++;
      prev = cur;
    }
  }

  /* CR1 期望值 0x002D = UE|TE|RE|RXNEIE（bit0/3/2/5）；BRR 期望 0x22A1 @115200/16x */
  BSP_UART_Printf("[uart] CR1=%04lX CR3=%04lX BRR=%04lX ISR=%08lX\r\n",
                  (unsigned long)USART1->CR1, (unsigned long)USART1->CR3,
                  (unsigned long)USART1->BRR, (unsigned long)USART1->ISR);
  BSP_UART_Printf("[uart] MODER=%08lX IDR=%04lX PA10=%lu edges/20ms=%lu\r\n",
                  (unsigned long)GPIOA->MODER, (unsigned long)GPIOA->IDR,
                  (unsigned long)((GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U),
                  (unsigned long)edges);
  /* ISER1 bit5 = USART1_IRQn(37) 使能位 */
  BSP_UART_Printf("[uart] ISER1=%08lX rxIrq=%lu drop=%lu head=%u tail=%u\r\n",
                  (unsigned long)NVIC->ISER[1], (unsigned long)s_rxIrqCnt,
                  (unsigned long)s_rxDropCnt, (unsigned)s_rxHead, (unsigned)s_rxTail);
  BSP_UART_Printf("[uart] gState=%u RxState=%u RxCnt=%u\r\n",
                  (unsigned)huart1.gState, (unsigned)huart1.RxState,
                  (unsigned)huart1.RxXferCount);
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
