/**
  ******************************************************************************
  * @file    bl_can.c
  * @brief   BL 的 FDCAN2 经典 CAN 驱动实现（轮询接收）
  *
  * 硬件与位时序（与既有工程 can.c / stm32g4xx_hal_msp.c 完全一致）：
  *   PB12 = FDCAN2_RX，PB13 = FDCAN2_TX（AF9）
  *   FDCAN 内核时钟 = PCLK1 = 150MHz
  *   500 kbit/s：NominalPrescaler=30 -> tq=200ns，10 tq/位；
  *                Seg1=7, Seg2=2 (+Sync 1) -> 采样点 80%，SJW=2
  *   FDCAN_FRAME_CLASSIC + AutoRetransmission=ENABLE + TxFifoQueueMode=FDCAN_TX_FIFO_OPERATION
  *
  * 与既有 can.c 的差异（刻意的）：
  *   - **不开中断**：BL 只在主循环 BL_CanPoll() 轮询 RX FIFO0，避免 BL 里引入
  *     中断优先级/回调依赖；因此不调用 HAL_FDCAN_ActivateNotification，也无需
  *     FDCAN2_IT0_IRQHandler。MspInit 里显式关闭中断线以防残留使能位。
  *   - 不存 Tx Event（无中断排空事件 FIFO 会溢出），TxEventFifoControl=FDCAN_NO_TX_EVENTS。
  *   - 过滤器只放行诊断请求 0x7E0；业务 ID 0x321/0x322 BL 完全不需要。
  ******************************************************************************
  */
#include "bl_can.h"
#include <string.h>   /* memset：M12 修复需要（与 bl_uart.c 对齐） */

/*--------------------------------------- 模块内部宏 --------------------------------------*/
#define BL_CAN_PAYLOAD_LEN     8U
#define BL_CAN_TX_FIFO_DEPTH   3U      /* FDCAN 消息 RAM 默认 Tx FIFO 深度 */
#define BL_CAN_STD_ID_MASK     0x7FFU
#define BL_CAN_PRESCALER       30U     /* 150MHz / (30 * 10) = 500 kbit/s */
#define BL_CAN_TIME_SEG1       7U
#define BL_CAN_TIME_SEG2       2U
#define BL_CAN_SJW             2U

/*--------------------------------------- 模块内部变量 ------------------------------------*/
FDCAN_HandleTypeDef hfdcan2;          /* FDCAN2 句柄（调试器可观察） */

/**
  * @brief  FDCAN2 底层初始化：内核时钟、GPIO、关闭中断线
  * @note   由 HAL_FDCAN_Init() 内部回调（HAL_FDCAN_MspInit 是 __weak）
  */
void HAL_FDCAN_MspInit(FDCAN_HandleTypeDef *hfdcan)
{
  GPIO_InitTypeDef         gpioInit   = {0};
  RCC_PeriphCLKInitTypeDef periphClk  = {0};

  if (hfdcan->Instance == FDCAN2)
  {
    /* FDCAN 内核时钟选择 PCLK1 = 150MHz（上面的位时序参数按此时钟计算） */
    periphClk.PeriphClockSelection = RCC_PERIPHCLK_FDCAN;
    periphClk.FdcanClockSelection  = RCC_FDCANCLKSOURCE_PCLK1;
    if (HAL_RCCEx_PeriphCLKConfig(&periphClk) != HAL_OK)
    {
      Error_Handler();
    }

    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_FDCAN_CLK_ENABLE();

    /* PB12 = FDCAN2_RX（接收发器 TXD），PB13 = FDCAN2_TX（接收发器 RXD），AF9 */
    gpioInit.Pin       = GPIO_PIN_12 | GPIO_PIN_13;
    gpioInit.Mode      = GPIO_MODE_AF_PP;
    gpioInit.Pull      = GPIO_NOPULL;
    gpioInit.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    gpioInit.Alternate = GPIO_AF9_FDCAN2;
    HAL_GPIO_Init(GPIOB, &gpioInit);

    /* BL 不用中断收帧：显式关闭中断线，避免上电残留使能位导致意外进中断 */
    HAL_NVIC_DisableIRQ(FDCAN2_IT0_IRQn);
  }
}

/**
  * @brief  FDCAN2 底层反初始化
  */
void HAL_FDCAN_MspDeInit(FDCAN_HandleTypeDef *hfdcan)
{
  if (hfdcan->Instance == FDCAN2)
  {
    HAL_NVIC_DisableIRQ(FDCAN2_IT0_IRQn);
    __HAL_RCC_FDCAN_CLK_DISABLE();
    HAL_GPIO_DeInit(GPIOB, GPIO_PIN_12 | GPIO_PIN_13);
  }
}

/**
  * @brief  初始化 FDCAN2（500k 经典 CAN，只收 0x7E0，启动）
  */
HAL_StatusTypeDef BL_CanInit(void)
{
  FDCAN_FilterTypeDef filter = {0};

  /* 【M12 修复，2026-10-07】先 memset 再逐项赋值，与 bl_uart.c 的做法对齐。
   *
   * 为什么必须 memset：hfdcan2 是文件级 static 变量，本函数被 main.c 的 recovery
   * 逻辑每 2s 重入一次。原先只靠"逐项赋值齐全"来保证句柄干净 —— 一旦 HAL 升级
   * 增加了字段、或有人漏写一个字段，残留的旧值就会静默生效。
   * bl_uart.c 曾因同类问题出现过 HardFault（ClockPrescaler 未赋值被 HAL 当查表
   * 下标用），本模块目前"恰好写全"属于运气，不是结构化保证。
   *
   * ⚠️ 位置很关键：memset 必须在下面 `hfdcan2.State == HAL_FDCAN_STATE_BUSY`
   *    判断**之后**才能做 —— 否则会把状态清零，导致 HAL 要求的前置条件被绕过。
   *    所以这里先读状态做判断，再 memset。
   */
  if (hfdcan2.State == HAL_FDCAN_STATE_BUSY)
  {
    if (HAL_FDCAN_Stop(&hfdcan2) != HAL_OK)
    {
      return HAL_ERROR;
    }
  }

  /* 状态已确认非 BUSY（Stop 也已把 State 复位），此时清零句柄是安全的 */
  (void)memset(&hfdcan2, 0, sizeof(hfdcan2));

  hfdcan2.Instance                  = FDCAN2;
  hfdcan2.Init.ClockDivider         = FDCAN_CLOCK_DIV1;
  hfdcan2.Init.FrameFormat          = FDCAN_FRAME_CLASSIC;   /* 经典 CAN，非 FD */
  hfdcan2.Init.Mode                 = FDCAN_MODE_NORMAL;
  hfdcan2.Init.AutoRetransmission   = ENABLE;                /* 硬件自动重发，保证送达 */
  hfdcan2.Init.TransmitPause        = DISABLE;
  hfdcan2.Init.ProtocolException    = ENABLE;
  hfdcan2.Init.NominalPrescaler     = BL_CAN_PRESCALER;
  hfdcan2.Init.NominalSyncJumpWidth = BL_CAN_SJW;
  hfdcan2.Init.NominalTimeSeg1      = BL_CAN_TIME_SEG1;
  hfdcan2.Init.NominalTimeSeg2      = BL_CAN_TIME_SEG2;
  hfdcan2.Init.DataPrescaler        = BL_CAN_PRESCALER;      /* 经典帧不用数据段，填合法值 */
  hfdcan2.Init.DataSyncJumpWidth    = BL_CAN_SJW;
  hfdcan2.Init.DataTimeSeg1         = BL_CAN_TIME_SEG1;
  hfdcan2.Init.DataTimeSeg2         = BL_CAN_TIME_SEG2;
  hfdcan2.Init.StdFiltersNbr        = 1U;
  hfdcan2.Init.ExtFiltersNbr        = 0U;
  hfdcan2.Init.TxFifoQueueMode      = FDCAN_TX_FIFO_OPERATION;

  if (HAL_FDCAN_Init(&hfdcan2) != HAL_OK)
  {
    return HAL_ERROR;
  }

  /* 接收过滤器：只放行诊断请求 ID 0x7E0（掩码 0x7FF 精确匹配） */
  filter.IdType       = FDCAN_STANDARD_ID;
  filter.FilterIndex  = 0U;
  filter.FilterType   = FDCAN_FILTER_MASK;
  filter.FilterConfig = FDCAN_FILTER_TO_RXFIFO0;
  filter.FilterID1    = BL_DIAG_REQ_ID;
  filter.FilterID2    = BL_CAN_STD_ID_MASK;

  if (HAL_FDCAN_ConfigFilter(&hfdcan2, &filter) != HAL_OK)
  {
    return HAL_ERROR;
  }

  /* 其余标准/扩展帧一律拒收（BL 不关心业务 ID 0x321/0x322） */
  if (HAL_FDCAN_ConfigGlobalFilter(&hfdcan2, FDCAN_REJECT, FDCAN_REJECT,
                                   FDCAN_FILTER_REMOTE, FDCAN_FILTER_REMOTE) != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (HAL_FDCAN_Start(&hfdcan2) != HAL_OK)
  {
    return HAL_ERROR;
  }

  return HAL_OK;
}

/**
  * @brief  发送一帧经典 CAN 数据帧（标准 ID）
  */
HAL_StatusTypeDef BL_CanSend(uint32_t stdId, const uint8_t *data, uint8_t len)
{
  FDCAN_TxHeaderTypeDef txHeader;
  uint8_t               txData[BL_CAN_PAYLOAD_LEN] = {0U};  /* 驱动按字拷贝：零填充防越界读 */
  uint8_t               i;

  if ((data == NULL) || (len > BL_CAN_PAYLOAD_LEN) || (stdId > BL_CAN_STD_ID_MASK))
  {
    return HAL_ERROR;
  }

  if (HAL_FDCAN_GetTxFifoFreeLevel(&hfdcan2) == 0U)
  {
    return HAL_BUSY;                              /* Tx FIFO 满：上层按需重试 */
  }

  for (i = 0U; i < len; i++)
  {
    txData[i] = data[i];
  }

  txHeader.Identifier          = stdId;
  txHeader.IdType              = FDCAN_STANDARD_ID;
  txHeader.TxFrameType         = FDCAN_DATA_FRAME;
  txHeader.DataLength          = len;                     /* V1.2.3 语义：原始 DLC(0~15) */
  txHeader.ErrorStateIndicator = FDCAN_ESI_ACTIVE;
  txHeader.BitRateSwitch       = FDCAN_BRS_OFF;
  txHeader.FDFormat            = FDCAN_CLASSIC_CAN;
  txHeader.TxEventFifoControl  = FDCAN_NO_TX_EVENTS;      /* 无中断排空事件 FIFO，不存事件 */
  txHeader.MessageMarker       = 0U;

  if (HAL_FDCAN_AddMessageToTxFifoQ(&hfdcan2, &txHeader, txData) != HAL_OK)
  {
    return HAL_ERROR;
  }

  return HAL_OK;
}

/**
  * @brief  轮询取走一帧接收报文（主循环调用）
  */
bool BL_CanPoll(BL_CanFrame *frame)
{
  FDCAN_RxHeaderTypeDef rxHeader;
  uint8_t               rxData[BL_CAN_PAYLOAD_LEN];
  uint8_t               i;

  if (frame == NULL)
  {
    return false;
  }

  if (HAL_FDCAN_GetRxMessage(&hfdcan2, FDCAN_RX_FIFO0, &rxHeader, rxData) != HAL_OK)
  {
    /* FIFO 为空时 HAL 返回 HAL_ERROR 并把 ErrorCode 粘滞为 FIFO_EMPTY；
     * BL 未使能中断，这里主动清掉，避免影响后续判断 */
    hfdcan2.ErrorCode = HAL_FDCAN_ERROR_NONE;
    return false;
  }

  frame->id  = rxHeader.Identifier;
  frame->len = (rxHeader.DataLength > BL_CAN_PAYLOAD_LEN) ? BL_CAN_PAYLOAD_LEN
                                                         : (uint8_t)rxHeader.DataLength;
  for (i = 0U; i < BL_CAN_PAYLOAD_LEN; i++)
  {
    frame->data[i] = rxData[i];
  }

  return true;
}

/**
  * @brief  Tx FIFO 是否已排空
  */
bool BL_CanTxIdle(void)
{
  return (HAL_FDCAN_GetTxFifoFreeLevel(&hfdcan2) >= BL_CAN_TX_FIFO_DEPTH);
}

/**
  * @brief  释放 FDCAN2（跳转 APP 前）
  */
void BL_CanDeInit(void)
{
  (void)HAL_FDCAN_DeInit(&hfdcan2);
}
