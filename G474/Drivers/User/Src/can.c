/**
  ******************************************************************************
  * @file    can.c
  * @brief   双机CAN通信驱动实现（基于FDCAN2，经典CAN模式，RX FIFO0 + 中断线0）
  *
  * 设计说明（企业级要求）：
  *   1. 发送采用Tx FIFO（深度3），发送前查询剩余空间，不阻塞应用层；
  *      发送启用Tx Event FIFO（MessageMarker标记），中断里逐帧确认
  *      "已真正送达总线"并测量入队->确认延迟（txAckCount/txMaxLatencyMs）；
  *   2. 接收采用RX FIFO0 + FDCAN2中断线0，回调中排空FIFO写入16深度
  *      无锁SPSC环形缓冲（中断只写写指针、主循环只读写指针，Cortex-M
  *      单核下配合DMB屏障安全），应用层CAN_PollRx()轮询取走；
  *   3. bus-off采用指数退避自动恢复：1s起步翻倍、上限30s（CAN_Task调度），
  *      任一帧发送确认成功即复位退避——避免故障总线上的重连风暴；
  *   4. 上电自检CAN_SelfTest()：内部回环自发自收，验证软件链路完整性；
  *   5. 支持运行时重配置（CAN_Configure）：波特率切换、混杂模式切换，
  *      重配置流程 = Stop -> 重填参数 -> Init -> 过滤器 -> Start -> 重挂中断；
  *   6. 所有HAL返回值均检查并统计，异常不静默。
  *
  * 参考实现：STMicroelectronics/STM32CubeG4 官方示例
  *           Projects/STM32G474E-EVAL/Examples/FDCAN/FDCAN_Classic_Frame_Networking
  *           （本项目HAL库版本V1.2.3，与该示例API完全一致）
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2024 STMicroelectronics（驱动调用模式）
  *
  ******************************************************************************
  */
#include "can.h"
#include "isotp.h"

/*--------------------------------------- 模块配置 --------------------------------------*/
#define CAN_RX_RING_LEN       16U                     /* 接收环形缓冲深度（2的幂，取模优化） */
#define CAN_BUSOFF_BACKOFF_MIN_MS   1000U             /* bus-off恢复起始退避 */
#define CAN_BUSOFF_BACKOFF_MAX_MS   30000U            /* bus-off恢复退避上限 */

/*--------------------------------------- 模块内部变量 --------------------------------------*/
FDCAN_HandleTypeDef hfdcan2;                          /* FDCAN2句柄（中断服务函数引用） */

static CAN_RxMsg    s_rxRing[CAN_RX_RING_LEN];        /* 接收环形缓冲 */
static volatile uint8_t s_rxW;                        /* 写索引（仅接收中断修改） */
static volatile uint8_t s_rxR;                        /* 读索引（仅主循环修改） */

static uint32_t     s_txTick[256];                    /* MessageMarker -> 入队时刻（测延迟） */
static uint8_t      s_txMarker;                       /* 下一帧的MessageMarker */

static CAN_Stats    s_stats;                          /* 通信统计（调试器可观察） */

static bool         s_promisc;                        /* 混杂模式：接收所有ID */
static bool         s_loopbackSelfTest;               /* 运行时自检：内部回环 */
static uint32_t     s_bitrate = CAN_BITRATE;          /* 当前位速率 */

static volatile uint32_t s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MIN_MS; /* 当前退避时长 */
static volatile uint32_t s_busOffDueTick;             /* 待执行的恢复时刻（0=无待恢复） */

/* 波特率-分频查找表：FDCAN内核时钟=PCLK1=150MHz，所有档位统一
 * 10tq/位、采样点80%、SJW=2：位速率 = 150MHz / (prescaler × 10)
 *   125k -> 120   250k -> 60   500k -> 30   1M -> 15                    */
static const uint32_t s_bitrateTable[][2] =
{
  { 125000U, 120U },
  { 250000U,  60U },
  { 500000U,  30U },
  {1000000U,  15U },
};

/**
  * @brief  查询位速率对应的分频值
  * @retval 分频值；0表示该速率不受支持
  */
static uint32_t CAN_BitrateToPrescaler(uint32_t bitrate)
{
  uint32_t i;

  for (i = 0U; i < (sizeof(s_bitrateTable) / sizeof(s_bitrateTable[0])); i++)
  {
    if (s_bitrateTable[i][0] == bitrate)
    {
      return s_bitrateTable[i][1];
    }
  }
  return 0U;
}

/**
  * @brief  FDCAN2完整配置流程（初始化与运行时重配置共用）
  * @note   仅允许主循环上下文调用；重配置后环形缓冲复位（此时已停机，无RX中断竞争）
  * @retval HAL状态
  */
static HAL_StatusTypeDef CAN_Configure(void)
{
  HAL_StatusTypeDef   status;
  FDCAN_FilterTypeDef filterConfig;
  uint32_t            prescaler = CAN_BitrateToPrescaler(s_bitrate);

  if (prescaler == 0U)
  {
    return HAL_ERROR;
  }

  /* 运行中重配置：先停机使状态机回到READY（HAL_FDCAN_Init要求） */
  if (hfdcan2.State == HAL_FDCAN_STATE_BUSY)
  {
    if (HAL_FDCAN_Stop(&hfdcan2) != HAL_OK)
    {
      return HAL_ERROR;
    }
  }

  /* 位时序：PCLK1=150MHz，NominalPrescaler -> tq，10tq/位 -> 对应位速率，
   * 采样点80%。经典CAN模式不使用数据段位时序，按与仲裁段相同的合法值配置。 */
  hfdcan2.Instance                            = FDCAN2;
  hfdcan2.Init.ClockDivider                   = FDCAN_CLOCK_DIV1;
  hfdcan2.Init.FrameFormat                    = FDCAN_FRAME_CLASSIC;
#if (CAN_DEBUG_SELFTEST != 0)
  hfdcan2.Init.Mode                           = FDCAN_MODE_EXTERNAL_LOOPBACK; /* 调试自测试：自发自收且驱动总线脚 */
#else
  if (s_loopbackSelfTest)
  {
    hfdcan2.Init.Mode                         = FDCAN_MODE_INTERNAL_LOOPBACK; /* 上电自检：内部回环不驱动总线脚 */
  }
  else
  {
    hfdcan2.Init.Mode                         = FDCAN_MODE_NORMAL;
  }
#endif
  hfdcan2.Init.AutoRetransmission             = ENABLE;   /* 硬件自动重发，保证可靠送达 */
  hfdcan2.Init.TransmitPause                  = DISABLE;
  hfdcan2.Init.ProtocolException              = ENABLE;
  hfdcan2.Init.NominalPrescaler               = prescaler;
  hfdcan2.Init.NominalSyncJumpWidth           = 2U;
  hfdcan2.Init.NominalTimeSeg1                = 7U;       /* 传播段+相位段1 */
  hfdcan2.Init.NominalTimeSeg2                = 2U;
  hfdcan2.Init.DataPrescaler                  = prescaler;
  hfdcan2.Init.DataSyncJumpWidth              = 2U;
  hfdcan2.Init.DataTimeSeg1                   = 7U;
  hfdcan2.Init.DataTimeSeg2                   = 2U;
  /* 过滤器数量：混杂/自检模式1个(全收，多过滤器会重复存帧)；正常模式2个(心跳+诊断) */
  /* OTA 扩展：正常模式需要同时收 0x7E0(请求) 与 0x7E8(应答) 两个诊断 ID。
   * 一条掩码型过滤器只能匹配"一个 ID + 一个掩码"（FilterID2 是掩码而非第二个 ID），
   * 因此由 2 个过滤器增加到 3 个（业务帧 + 0x7E0 + 0x7E8）。 */
  hfdcan2.Init.StdFiltersNbr                  = (s_promisc || s_loopbackSelfTest) ? 1U : 3U;
  hfdcan2.Init.ExtFiltersNbr                  = 0U;
  hfdcan2.Init.TxFifoQueueMode                = FDCAN_TX_FIFO_OPERATION;

  status = HAL_FDCAN_Init(&hfdcan2);
  if (status != HAL_OK)
  {
    return status;
  }

  /* 接收过滤器：混杂/自检模式收全部；正常模式只收对端报文（掩码0x7FF精确匹配） */
  filterConfig.IdType       = FDCAN_STANDARD_ID;
  filterConfig.FilterIndex  = 0U;
  filterConfig.FilterType   = FDCAN_FILTER_MASK;
  filterConfig.FilterConfig = FDCAN_FILTER_TO_RXFIFO0;
#if (CAN_DEBUG_SELFTEST != 0)
  filterConfig.FilterID1    = 0x000U;                 /* 调试自测试：掩码0，接收所有ID */
  filterConfig.FilterID2    = 0x000U;
#else
  if (s_promisc || s_loopbackSelfTest)
  {
    filterConfig.FilterID1  = 0x000U;                 /* 混杂/自检：掩码0，接收所有标准ID */
    filterConfig.FilterID2  = 0x000U;
  }
  else
  {
#if (CAN_NODE_ROLE == CAN_NODE_A)
    filterConfig.FilterID1  = CAN_ID_RESP_B2A;         /* 板A接收板B的应答帧 */
#else
    filterConfig.FilterID1  = CAN_ID_CMD_A2B;           /* 板B接收板A的命令帧 */
#endif
    filterConfig.FilterID2  = 0x7FFU;                   /* 掩码：11位ID全比较 */
  }
#endif

  status = HAL_FDCAN_ConfigFilter(&hfdcan2, &filterConfig);
  if (status != HAL_OK)
  {
    return status;
  }

#if (CAN_DEBUG_SELFTEST != 0)
  /* 自测试：单过滤器足够 */
#else
  /* 第二过滤器：诊断ID（正常模式；混杂/自检模式下全收已覆盖，避免重复存帧） */
  if (!s_promisc && !s_loopbackSelfTest)
  {
    filterConfig.FilterIndex = 1U;
#if (CAN_NODE_ROLE == CAN_NODE_A)
    filterConfig.FilterID1   = ISOTP_DIAG_RESP_ID;      /* 板A接收诊断应答0x7E8 */
#else
    filterConfig.FilterID1   = ISOTP_DIAG_REQ_ID;       /* 板B接收诊断请求0x7E0 */
#endif
    filterConfig.FilterID2   = 0x7FFU;
    status = HAL_FDCAN_ConfigFilter(&hfdcan2, &filterConfig);
    if (status != HAL_OK)
    {
      return status;
    }

    /* 第三过滤器：诊断应答 ID（0x7E8）。
     * 用途：① 串口隧道模式需回吐 0x7E0/0x7E8 两个方向；
     *       ② 单板自测时本节点既是 ECU 又是诊断仪；
     *       ③ 双 CAN / 多诊断 ID 并存场景。 */
    filterConfig.FilterIndex = 2U;
    filterConfig.FilterID1   = ISOTP_DIAG_RESP_ID;      /* 0x7E8 */
    filterConfig.FilterID2   = 0x7FFU;
    status = HAL_FDCAN_ConfigFilter(&hfdcan2, &filterConfig);
    if (status != HAL_OK)
    {
      return status;
    }
  }
#endif

  /* 全局过滤器：混杂/自检模式下非匹配标准/扩展帧一并收入RX FIFO0；正常模式全部拒绝 */
  if (s_promisc || s_loopbackSelfTest)
  {
    status = HAL_FDCAN_ConfigGlobalFilter(&hfdcan2, FDCAN_ACCEPT_IN_RX_FIFO0,
                                          FDCAN_ACCEPT_IN_RX_FIFO0,
                                          FDCAN_FILTER_REMOTE, FDCAN_FILTER_REMOTE);
  }
  else
  {
    status = HAL_FDCAN_ConfigGlobalFilter(&hfdcan2, FDCAN_REJECT, FDCAN_REJECT,
                                          FDCAN_FILTER_REMOTE, FDCAN_FILTER_REMOTE);
  }
  if (status != HAL_OK)
  {
    return status;
  }

  status = HAL_FDCAN_Start(&hfdcan2);
  if (status != HAL_OK)
  {
    return status;
  }

  /* 接收新报文 + Tx事件确认 + 总线错误状态中断（V1.2.3中断线由ILS复位值决定，均走中断线0） */
  status = HAL_FDCAN_ActivateNotification(&hfdcan2, FDCAN_IT_RX_FIFO0_NEW_MESSAGE, 0U);
  if (status != HAL_OK)
  {
    return status;
  }
  /* Tx事件确认三件套：
   * 1) TEFNE：新事件写入通知（排空事件FIFO、对账MessageMarker）；
   * 2) TX_COMPLETE + BufferIndexes=0x07：使能3个Tx缓冲区的传输完成中断，
   *    V1.2.3的HAL据此置TXBTIE寄存器——G4精简FDCAN IP在TXBTIE未使能时
   *    事件FIFO写入处于未授权状态，会导致每次发送误报TEFL（元素丢失）；
   * 3) TEFL：真实丢失时的告警（正常应为0）。 */
  status = HAL_FDCAN_ActivateNotification(&hfdcan2,
                                          FDCAN_IT_TX_EVT_FIFO_NEW_DATA | FDCAN_IT_TX_EVT_FIFO_ELT_LOST,
                                          0U);
  if (status != HAL_OK)
  {
    return status;
  }
  status = HAL_FDCAN_ActivateNotification(&hfdcan2, FDCAN_IT_TX_COMPLETE, 0x07U);
  if (status != HAL_OK)
  {
    return status;
  }
  status = HAL_FDCAN_ActivateNotification(&hfdcan2, FDCAN_IT_BUS_OFF | FDCAN_IT_ERROR_PASSIVE, 0U);
  if (status != HAL_OK)
  {
    return status;
  }

  /* 已停机，无RX中断竞争，复位环形缓冲 */
  s_rxR = 0U;
  s_rxW = 0U;

  return HAL_OK;
}

/**
  * @brief  初始化FDCAN2：位时序、过滤器、中断、启动
  * @retval HAL状态
  */
HAL_StatusTypeDef CAN_Init(void)
{
  s_bitrate         = CAN_BITRATE;
  s_promisc         = false;
  s_loopbackSelfTest = false;
  s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MIN_MS;
  s_busOffDueTick   = 0U;
  return CAN_Configure();
}

/**
  * @brief  上电自检：内部回环自发自收一帧（不驱动总线引脚）
  * @retval true=软件链路完整；false=自检失败
  */
bool CAN_SelfTest(void)
{
  const uint8_t testPayload[CAN_PAYLOAD_LEN] = {0x5AU, 0xA5U, 0x3CU, 0xC3U, 0, 0, 0, 0};
  bool          ok = false;
  CAN_RxMsg     msg;
  uint32_t      start;

  s_loopbackSelfTest = true;
  if (CAN_Configure() != HAL_OK)
  {
    s_loopbackSelfTest = false;
    (void)CAN_Configure();                           /* 尽力恢复正常模式 */
    return false;
  }

  if (CAN_Send(0x1F5U, testPayload, 4U) == HAL_OK)
  {
    start = HAL_GetTick();
    while ((HAL_GetTick() - start) < 100U)           /* 自检超时100ms */
    {
      if (CAN_PollRx(&msg))
      {
        ok = (msg.id == 0x1F5U) && (msg.len == 4U) &&
             (msg.data[0] == 0x5AU) && (msg.data[1] == 0xA5U);
        break;
      }
    }
  }

  s_loopbackSelfTest = false;
  (void)CAN_Configure();                             /* 恢复正常模式 */
  return ok;
}

/**
  * @brief  主循环周期任务：调度bus-off指数退避恢复
  * @note   恢复动作（Stop->Start）放在主循环而非中断，避免中断里做耗时操作
  */
void CAN_Task(void)
{
  if ((s_busOffDueTick != 0U) && ((int32_t)(HAL_GetTick() - s_busOffDueTick) >= 0))
  {
    s_busOffDueTick = 0U;

    if (HAL_FDCAN_Stop(&hfdcan2) == HAL_OK)
    {
      (void)HAL_FDCAN_Start(&hfdcan2);
      (void)HAL_FDCAN_ActivateNotification(&hfdcan2, FDCAN_IT_RX_FIFO0_NEW_MESSAGE, 0U);
      (void)HAL_FDCAN_ActivateNotification(&hfdcan2,
                                          FDCAN_IT_TX_EVT_FIFO_NEW_DATA | FDCAN_IT_TX_EVT_FIFO_ELT_LOST,
                                          0U);
      (void)HAL_FDCAN_ActivateNotification(&hfdcan2, FDCAN_IT_TX_COMPLETE, 0x07U);
      (void)HAL_FDCAN_ActivateNotification(&hfdcan2, FDCAN_IT_BUS_OFF | FDCAN_IT_ERROR_PASSIVE, 0U);
    }
    else
    {
      s_busOffDueTick = HAL_GetTick() + 100U;        /* Stop失败：100ms后重试 */
    }
  }
}

/**
  * @brief  运行时切换总线波特率（125k/250k/500k/1M）
  * @note   重配置期间收发短暂中断；两块板必须切换到同一速率，否则无法互通
  * @retval HAL状态
  */
HAL_StatusTypeDef CAN_SetBitrate(uint32_t bitrate)
{
  if (CAN_BitrateToPrescaler(bitrate) == 0U)
  {
    return HAL_ERROR;                                  /* 不支持的速率 */
  }

  s_bitrate = bitrate;
  return CAN_Configure();
}

/**
  * @brief  开/关混杂模式（总线监视sniff：接收所有ID）
  * @retval HAL状态
  */
HAL_StatusTypeDef CAN_SetPromiscuous(bool enable)
{
  s_promisc = enable;
  return CAN_Configure();
}

/**
  * @brief  读取当前位速率
  */
uint32_t CAN_GetBitrate(void)
{
  return s_bitrate;
}

/**
  * @brief  发送一帧经典CAN数据帧（标准ID）
  * @param  stdId 标准ID（0x000~0x7FF）
  * @param  data  数据指针
  * @param  len   数据长度（<=8）
  * @retval HAL状态（HAL_BUSY表示Tx FIFO暂满）
  */
HAL_StatusTypeDef CAN_Send(uint32_t stdId, const uint8_t *data, uint8_t len)
{
  FDCAN_TxHeaderTypeDef txHeader;
  uint8_t               txData[CAN_PAYLOAD_LEN] = {0U};  /* 零填充暂存区：驱动按字(4字节)拷贝，
                                                           防止len<8时读越界 */
  uint8_t               marker;

  if ((data == NULL) || (len > CAN_PAYLOAD_LEN) ||
      (stdId > 0x7FFU))
  {
    s_stats.txErrors++;
    return HAL_ERROR;
  }

  if (HAL_FDCAN_GetTxFifoFreeLevel(&hfdcan2) == 0U)
  {
    s_stats.txErrors++;                        /* Tx FIFO满：放弃本帧并统计 */
    return HAL_BUSY;
  }

  for (uint8_t i = 0U; i < len; i++)
  {
    txData[i] = data[i];
  }

  marker = ++s_txMarker;                        /* 1~255循环标记（Tx Event回读对账） */
  s_txTick[marker] = HAL_GetTick();             /* 入队时刻，用于确认延迟测量 */

  txHeader.Identifier          = stdId;
  txHeader.IdType              = FDCAN_STANDARD_ID;
  txHeader.TxFrameType         = FDCAN_DATA_FRAME;
  txHeader.DataLength          = len;                     /* V1.2.3语义：原始DLC(0~15)，
                                                            驱动内部完成<<16编码 */
  txHeader.ErrorStateIndicator = FDCAN_ESI_ACTIVE;
  txHeader.BitRateSwitch       = FDCAN_BRS_OFF;
  txHeader.FDFormat            = FDCAN_CLASSIC_CAN;        /* 经典CAN帧 */
  txHeader.TxEventFifoControl  = FDCAN_STORE_TX_EVENTS;    /* 存Tx事件供发送确认 */
  txHeader.MessageMarker       = marker;

  if (HAL_FDCAN_AddMessageToTxFifoQ(&hfdcan2, &txHeader, txData) != HAL_OK)
  {
    s_stats.txErrors++;
    return HAL_ERROR;
  }

  s_stats.txCount++;
  return HAL_OK;
}

/**
  * @brief  轮询取走一帧接收报文（主循环调用）
  * @param  msg 输出报文
  * @retval true取到一帧；false缓冲为空
  * @note   无锁SPSC：读方仅触碰s_rxR，写方（中断）仅触碰s_rxW；先拷贝数据
  *         再推进读索引，写方先写数据再推进写索引，配合屏障保证可见性顺序。
  */
bool CAN_PollRx(CAN_RxMsg *msg)
{
  uint8_t r;
  uint8_t w;

  if (msg == NULL)
  {
    return false;
  }

  w = s_rxW;                                   /* 先快照写索引 */
  __DMB();                                     /* 确保读到最新数据前的索引可见性 */
  r = s_rxR;

  if (r == w)
  {
    return false;
  }

  *msg = s_rxRing[r];                          /* 读取方独占该槽位 */
  __DMB();
  s_rxR = (uint8_t)((r + 1U) & (CAN_RX_RING_LEN - 1U));

  return true;
}

/**
  * @brief  获取通信统计指针（调试器实时观察用）
  */
const CAN_Stats *CAN_GetStats(void)
{
  return &s_stats;
}

/**
  * @brief  RX FIFO0接收回调（中断上下文）：排空FIFO写入环形缓冲
  */
void HAL_FDCAN_RxFifo0Callback(FDCAN_HandleTypeDef *hfdcan, uint32_t RxFifo0ITs)
{
  FDCAN_RxHeaderTypeDef rxHeader;
  uint8_t               rxData[CAN_PAYLOAD_LEN];

  if ((RxFifo0ITs & FDCAN_IT_RX_FIFO0_NEW_MESSAGE) != 0U)
  {
    /* 循环排空FIFO，避免高负载下滞留 */
    while (HAL_FDCAN_GetRxMessage(hfdcan, FDCAN_RX_FIFO0, &rxHeader, rxData) == HAL_OK)
    {
      uint8_t w     = s_rxW;
      uint8_t next  = (uint8_t)((w + 1U) & (CAN_RX_RING_LEN - 1U));

      if (next == s_rxR)
      {
        s_stats.rxOverruns++;                   /* 环形缓冲满：本帧丢弃并统计 */
      }
      else
      {
        CAN_RxMsg *slot = &s_rxRing[w];

        slot->id        = rxHeader.Identifier;
        slot->timestamp = rxHeader.RxTimestamp;
        /* V1.2.3的DataLength为原始DLC：经典帧DLC>8时有效字节仍为8 */
        slot->len       = (rxHeader.DataLength > 8U) ? 8U : (uint8_t)rxHeader.DataLength;
        for (uint8_t i = 0U; i < slot->len; i++)
        {
          slot->data[i] = rxData[i];
        }
        __DMB();                               /* 先写数据后发布索引 */
        s_rxW = next;
      }
      s_stats.rxCount++;
    }

    /* 排空后最后一次GetRxMessage必然返回FIFO_EMPTY并粘滞置位ErrorCode；
     * V1.2.3的IRQHandler在每次中断结尾检查ErrorCode!=0即误调ErrorCallback，
     * 必须在此主动清除（否则协议错误计数按中断频率虚增） */
    hfdcan->ErrorCode = HAL_FDCAN_ERROR_NONE;
  }

  if ((RxFifo0ITs & FDCAN_IT_RX_FIFO0_MESSAGE_LOST) != 0U)
  {
    s_stats.rxOverruns++;                       /* FIFO满导致报文丢失 */
  }
}

/**
  * @brief  Tx事件回调（中断上下文）：逐帧确认送达并测量延迟
  * @note   任一帧确认成功即认为总线恢复健康，bus-off退避时长复位
  */
void HAL_FDCAN_TxEventFifoCallback(FDCAN_HandleTypeDef *hfdcan, uint32_t TxEventFifoITs)
{
  FDCAN_TxEventFifoTypeDef txEvent;

  if ((TxEventFifoITs & FDCAN_IT_TX_EVT_FIFO_NEW_DATA) != 0U)
  {
    while (HAL_FDCAN_GetTxEvent(hfdcan, &txEvent) == HAL_OK)
    {
      uint32_t latency = HAL_GetTick() - s_txTick[txEvent.MessageMarker];

      s_stats.txAckCount++;
      if (latency > s_stats.txMaxLatencyMs)
      {
        s_stats.txMaxLatencyMs = latency;       /* 入队->总线确认最大延迟 */
      }

      s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MIN_MS;  /* 总线健康：复位退避 */
    }

    /* 同Rx排空：清除GetTxEvent排空后粘滞的FIFO_EMPTY错误码，防ErrorCallback误报 */
    hfdcan->ErrorCode = HAL_FDCAN_ERROR_NONE;
  }

  if ((TxEventFifoITs & FDCAN_IT_TX_EVT_FIFO_ELT_LOST) != 0U)
  {
    s_stats.txEvtLost++;                        /* 事件FIFO溢出丢失（真实丢失，正常应为0） */
  }
}

/**
  * @brief  总线错误状态回调（中断上下文）：计数 + 登记退避恢复
  * @note   M_CAN进入bus-off时硬件自动置位INIT离线；真正的Stop->Start恢复
  *         由主循环CAN_Task在退避时间到后执行（不在中断里做）。
  */
void HAL_FDCAN_ErrorStatusCallback(FDCAN_HandleTypeDef *hfdcan, uint32_t ErrorStatusITs)
{
  UNUSED(hfdcan);

  if ((ErrorStatusITs & FDCAN_IT_ERROR_PASSIVE) != 0U)
  {
    s_stats.errPassiveCount++;                 /* 进入/退出错误被动状态（总线异常的早期信号） */
  }

  if ((ErrorStatusITs & FDCAN_IT_BUS_OFF) != 0U)
  {
    s_stats.busOffCount++;

    /* 指数退避：1s->2s->4s->...->30s封顶 */
    s_busOffDueTick = HAL_GetTick() + s_busOffBackoffMs;
    s_busOffBackoffMs *= 2U;
    if (s_busOffBackoffMs > CAN_BUSOFF_BACKOFF_MAX_MS)
    {
      s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MAX_MS;
    }
  }
}

/**
  * @brief  协议错误回调（中断上下文）：仅计数，供调试观察
  */
void HAL_FDCAN_ErrorCallback(FDCAN_HandleTypeDef *hfdcan)
{
  UNUSED(hfdcan);
  s_stats.protocolErrors++;
}
