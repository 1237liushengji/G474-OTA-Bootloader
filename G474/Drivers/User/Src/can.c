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
#include "bsp_uart.h"        /* 【2026-10-10 诊断】BSP_UART_BootMark */

/*--------------------------------------- 模块配置 --------------------------------------*/
#define CAN_RX_RING_LEN       16U                     /* 接收环形缓冲深度（2的幂，取模优化） */
#define CAN_BUSOFF_BACKOFF_MIN_MS   1000U             /* bus-off恢复起始退避 */
#define CAN_BUSOFF_BACKOFF_MAX_MS   30000U            /* bus-off恢复退避上限 */

/* 【2026-10-11 临时诊断开关】1 = CAN_SelfTest 跳过 CAN_Send 调用。
 * 用于判定 "S2 之后挂死" 是否发生在 CAN_Send / TXBAR 路径。
 * 定位完成后必须改回 0（或整段删除）。 */
#define CAN_SELFTEST_SKIP_SEND      0

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

/* 【2026-10-18 新增】发送停滞看门狗。
 * 场景：总线异常使 TEC 升入 error-passive（128~255）但**未达 bus-off(256)**。
 *   此时 AutoRetransmission=ENABLE 会让未送达的帧无限重传、永久占住 Tx 缓冲，
 *   于是 HAL_FDCAN_GetTxFifoFreeLevel() 恒为 0 → CAN_Send 恒返回 HAL_BUSY
 *   → ISO-TP 永远发不出去，整个 OTA 静默死锁。
 *   已有的 bus-off 恢复（Stop→Start）只在 TEC≥256 时触发，救不了 error-passive。
 * 对策：FIFO 连续满超过 CAN_TX_STALL_MS 就主动 Abort 全部待发缓冲，
 *   把"卡住不动的帧"丢掉换回可用空间，让上层按自己的超时逻辑重试。 */
#define CAN_TX_STALL_MS        500U
static uint32_t s_txStallSince;                       /* 首次观察到FIFO满的时刻(0=正常) */
volatile uint32_t g_canTxStallAborts;                 /* 触发中止次数（SWD可读） */

/* 【2026-10-12】轮询接收排空函数的自述声明。
 * CAN_SelfTest()（回环自检）会用它主动从 RX FIFO0 取帧，
 * 而其定义位于文件后部，故在此前置声明。 */
static void CAN_PollRxFifo0(void);

/* 【2026-10-11 新增】无串口诊断快照：SWD 直读 RAM，绕开 P2 串口不可靠的问题。
 * 定义在文件作用域（非 static）以便从 .map 查到地址。 */
CAN_DiagSnap_t g_canDiag;

/**
  * @brief  抓取一次 FDCAN2 + 芯片身份的快照到 g_canDiag（供 SWD 读取）
  * @param  stage 阶段码（CAN_DIAG_STAGE_*）
  * @note   纯寄存器读，不写任何 FDCAN 寄存器，不影响被测对象。
  *         对 0x4000A750 的读是"消息 RAM 可读性探针"（不写，避免副作用）。
  */
void CAN_DiagCapture(uint32_t stage)
{
  g_canDiag.magic          = 0xCA9D1A60U;
  g_canDiag.stage          = stage;
  g_canDiag.cccr           = FDCAN2->CCCR;
  g_canDiag.psr            = FDCAN2->PSR;
  g_canDiag.nbtp           = FDCAN2->NBTP;      /* ← 关键：0 表示位时序从未写入 */
  g_canDiag.txbc           = FDCAN2->TXBC;
  g_canDiag.txfqs          = FDCAN2->TXFQS;
  g_canDiag.txbrp          = FDCAN2->TXBRP;
  g_canDiag.msg_ram_a750   = *(volatile uint32_t *)0x4000A750U; /* FDCAN2 消息RAM 基址 */
  g_canDiag.dbgmcu_idcode  = *(volatile uint32_t *)0xE0042000U; /* DEV_ID/REV_ID ← 查 errata */
}

/* 波特率-分频查找表：FDCAN 内核时钟 = **HSE = 8MHz**（见 stm32g4xx_hal_msp.c），
 * 统一 **16tq/位**（Sync=1 + TSEG1=11 + TSEG2=4）、采样点 75%、SJW=2：
 *   位速率 = 8MHz / (prescaler × 16)
 *   125k -> 4    250k -> 2    500k -> 1    1M -> 不可达（表中置 0 = 不支持）
 * 【2026-10-11】1M 档在 8MHz+16tq 下需 prescaler=0.5，不可达；
 * 表中以 0 表示"不支持"，CAN_BitrateToPrescaler() 返回 0 时上层显式失败，
 * 不会静默跑成错误速率。 */
static const uint32_t s_bitrateTable[][2] =
{
  { 125000U, 4U },
  { 250000U, 2U },
  { 500000U, 1U },
  {1000000U, 0U },
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

  /* 位时序：内核时钟 = **HSE = 8MHz**（见 stm32g4xx_hal_msp.c），不经 APB 分频，
   * 统一 **16tq/位**（Sync 1 + TSEG1 11 + TSEG2 4）、采样点 **75%**、SJW=2。
   *   位速率 = 8MHz / (prescaler × 16)
   *   125k -> 4    250k -> 2    500k -> 1    1M -> 不可达（表中置 0）
   *
   * 【2026-10-11 修复】原参数 TSEG1=13/TSEG2=2 同样是 16tq，波特率也对，
   * 但采样点是 (1+13)/16 = 87.5%，偏离 CAN 推荐的 75%~87.5% 区间上限，
   * 长总线/高负载下位同步裕度不足。改为 11/4 取得 75%。
   *
   * 【重要历史】2026-10-10 曾把内核时钟从 PCLK1=150MHz 改成 HSE 8MHz，
   * 当时的"元凶是内核时钟超频"结论**建立在 HAL_FDCAN_Init 被删空的错误基础上**
   * （见 docs/13）。真正原因是 HAL 库被插桩时误删了 NBTP/TXBC/CCCR 配置段，
   * 导致位时序寄存器**从未被写入**。现已还原 HAL 并复核本参数。 */
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
  hfdcan2.Init.NominalTimeSeg1                = 11U;      /* 传播段+相位段1（16tq/位，采样点75%） */
  hfdcan2.Init.NominalTimeSeg2                = 4U;
  hfdcan2.Init.DataPrescaler                  = prescaler;
  hfdcan2.Init.DataSyncJumpWidth              = 2U;
  hfdcan2.Init.DataTimeSeg1                   = 11U;
  hfdcan2.Init.DataTimeSeg2                   = 4U;
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

  /* 【2026-10-12 关键修复】不再挂任何 FDCAN 中断，全部改为 CAN_Task 主循环轮询。
   *
   * 故障现象：两块板各跑 3~5s 就被 IWDG 复位，串口心跳停在
   *   [hb] t=5233 st=11 n=348276 —— 主循环停摆 + SysTick 停走。
   *
   * 定位过程（板上 SWD 冻结态实测）：
   *   - PC=0x0800C3BE（`E7FE` = `b .` 死循环，APP 的 Default_Handler 弱桩海）
   *   - XPSR=0x61000066 → IPSR=102 = 异常号 → IRQ86 = FDCAN2_IT0
   *   - NVIC IABR[2]=0x00400000（IRQ86 活动）、ISER[2]=0x00400000（已使能）
   *   - 核心寄存器 SP=0x20003098 处压着完整异常栈帧，xPSR 的 IPSR=0（线程态被抢断）
   *   - 但 g_fdcanIsrCnt==0 → FDCAN2_IT0_IRQHandler 函数体一次都没执行
   *   - FDCAN2 ECR=0x0013FC00（REC=124）、PSR=0x0000076A（LEC=2 Form error + EP + EW）
   *     → CAN 处于 error-passive，错误类中断源持续活跃
   *
   * 决定性二分实验（未改任何代码，只在板上写寄存器）：
   *   复位 -> 等 APP 起来 -> 写 NVIC ICER[2]=0（禁用 IRQ64~95，含 IRQ86）-> 放行
   * 结果：40 秒串口抓包内心跳一路跑到 t=5234 / n=348291（正是原冻结点），
   *       主循环稳定 16k 次/秒，全程无复位。→ 确认 IRQ86 即元凶。
   *
   * 为何 ISR 不执行却停在 Default_Handler：IRQ86 使能位在 MspInit 里置位，
   * 而此刻 APP 的 FDCAN2 尚未 Start，中断线一旦来请求即被服务；
   * 首次服务时 CPU 走的是弱桩别名（0x0800C3BF 一带的 `E7FE` 海），
   * 进入后永不返回 → 优先级 1 > SysTick 15 → 全系统冻结 → 3s IWDG 复位 → 死循环。
   *
   * 规避方式：NVIC 层面禁用（见 hal_msp.c）+ 纯轮询。CAN 负载为板间 OTA
   * 控制流（数百字节级），轮询完全够用，且彻底消除优先级饥饿风险。 */
  /* 不调用 HAL_FDCAN_ActivateNotification()；CAN_Task 负责排空 FIFO 与事件。 */
  hfdcan2.ErrorCode = HAL_FDCAN_ERROR_NONE;      /* 清启动期可能粘滞的错误码 */

  /* 已启动，复位接收环形缓冲 */
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
  *
  * 【2026-10-10 曾错误地跳过本项自检】当时的结论是"回环模式下写 TXBAR 会
  * 挂死 AHB"，并据此把回环发送整段删除。2026-10-11 复查发现真正原因是
  * **HAL_FDCAN_Init 被插桩时误删了 NBTP/TXBC/CCCR 配置段**（导致位时序
  * 寄存器从未写入、控制器未配置），与"回环"无关（见 docs/13）。
  * HAL 还原后，回环自发自收是验证 FDCAN 软件链路最直接的手段，故恢复。
  */
bool CAN_SelfTest(void)
{
  const uint8_t testPayload[CAN_PAYLOAD_LEN] = {0x5AU, 0xA5U, 0x3CU, 0xC3U, 0, 0, 0, 0};
  bool          ok = false;
  CAN_RxMsg     msg;
  uint32_t      start;

  s_loopbackSelfTest = true;
  BSP_UART_BootMark("S1: before CAN_Configure(loopback)");
  if (CAN_Configure() != HAL_OK)
  {
    s_loopbackSelfTest = false;
    (void)CAN_Configure();                           /* 尽力恢复正常模式 */
    BSP_UART_BootMark("S2b: loopback cfg FAILED");
    return false;
  }
  BSP_UART_BootMark("S2: after CAN_Configure(loopback)");

  /* 【2026-10-11 二次定位】S2 之后主循环再无任何输出（3s 后 IWDG 复位循环）。
   * 串口打点会因为"中断风暴饿死主循环"而丢失，故同时把步号写进
   * g_canDiag.stage —— 它是 .bss 变量，复位后由启动代码清零，
   * 但程序一旦跑过该点就会留下痕迹；用 SWD 在复位后 1s 左右读取即可
   * 判读"最后成功走到哪一步"。
   * 步号分配（与文件顶部的 CAN_DIAG_* 宏无关，这里就地约定）：
   *   20 = 已过 S2（loopback 配置完成）
   *   21 = 即将调 CAN_Send
   *   22 = CAN_Send 已返回
   *   23 = 进入 RX 轮询
   *   24 = RX 轮询结束
   *   25 = 即将恢复 normal 模式
   *   26 = 已恢复 normal 模式（自检流程走完） */
  g_canDiag.stage = 20U;
  BSP_UART_BootMark("S2a: state/TXFQS check");

  /* 回环自发自收：内部回环模式下 TX 不入总线，RX 直接收到自己发的帧 */
  {
    HAL_StatusTypeDef send_st;

#if (CAN_SELFTEST_SKIP_SEND != 0)
    BSP_UART_BootMark("S2b: SKIP CAN_Send (experiment)");
    send_st = HAL_ERROR;
#else
    g_canDiag.stage = 21U;
    BSP_UART_BootMark("S2b: before CAN_Send");
    send_st = CAN_Send(CAN_ID_CMD_A2B, testPayload, CAN_PAYLOAD_LEN);
    g_canDiag.stage = 22U;
    BSP_UART_BootMark(send_st == HAL_OK ? "S2c: CAN_Send OK" : "S2c: CAN_Send FAIL");
#endif

    if (send_st == HAL_OK)
    {
      g_canDiag.stage = 23U;
      BSP_UART_BootMark("S3: before rx poll");
      start = HAL_GetTick();
      while ((HAL_GetTick() - start) < 50U)          /* 最长等 50ms，不无限阻塞 */
      {
        /* 【2026-10-12】中断已停用，回环帧必须由轮询主动从 FIFO0 取出，
         * 否则 CAN_PollRx 永远看不到数据（环形缓冲由 CAN_PollRxFifo0 填充） */
        CAN_PollRxFifo0();
        if (CAN_PollRx(&msg))
        {
          if ((msg.id == CAN_ID_CMD_A2B) && (msg.len == CAN_PAYLOAD_LEN))
          {
            ok = true;                               /* ID 与长度对上即认为链路通 */
          }
          break;
        }
      }
      g_canDiag.stage = 24U;
      BSP_UART_BootMark("S3b: after rx poll");
    }
  }
  BSP_UART_BootMark(ok ? "S4: loopback RX OK" : "S4: loopback RX TIMEOUT");

  s_loopbackSelfTest = false;
  g_canDiag.stage = 25U;
  BSP_UART_BootMark("S5: before restore CAN_Configure");
  if (CAN_Configure() != HAL_OK)                     /* 恢复正常模式 */
  {
    BSP_UART_BootMark("S5b: restore cfg FAILED");
    return false;
  }
  g_canDiag.stage = 26U;
  BSP_UART_BootMark("S6: after restore");
  return ok;
}

/**
  * @brief  主循环周期任务：调度bus-off指数退避恢复
  * @note   恢复动作（Stop->Start）放在主循环而非中断，避免中断里做耗时操作
  */
/**
  * @brief  轮询排空 RX FIFO0（主循环上下文，非中断）
  * @note   【2026-10-12】替代原 HAL_FDCAN_RxFifo0Callback 的中断路径。
  *         由 CAN_Task 每轮调用；逻辑与原回调一致，仅上下文不同。
  */
static void CAN_PollRxFifo0(void)
{
  FDCAN_HandleTypeDef *hfdcan = &hfdcan2;
  FDCAN_RxHeaderTypeDef rxHeader;
  uint8_t               rxData[CAN_PAYLOAD_LEN];
  uint32_t              guard;

  /* 判断 FIFO0 是否有新报文（读 RXF0S 的 F0FL 位） */
  if ((hfdcan->Instance->IR & FDCAN_IT_RX_FIFO0_NEW_MESSAGE) == 0U)
  {
    /* FIFO 满导致丢失：IR 中 MESSAGE_LOST 置位则清标志并计数 */
    if ((hfdcan->Instance->IR & FDCAN_IT_RX_FIFO0_MESSAGE_LOST) != 0U)
    {
      __HAL_FDCAN_CLEAR_FLAG(hfdcan, FDCAN_FLAG_RX_FIFO0_MESSAGE_LOST);
      s_stats.rxOverruns++;
    }
    return;
  }

  __HAL_FDCAN_CLEAR_FLAG(hfdcan, FDCAN_FLAG_RX_FIFO0_NEW_MESSAGE);

  /* 硬上限 16 次：FIFO 深度仅 3，绝不允许在主循环里失控循环 */
  guard = 0U;
  while ((guard++ < 16U) &&
         (HAL_FDCAN_GetRxMessage(hfdcan, FDCAN_RX_FIFO0, &rxHeader, rxData) == HAL_OK))
  {
    uint8_t w     = s_rxW;
    uint8_t next  = (uint8_t)((w + 1U) & (CAN_RX_RING_LEN - 1U));

    if (next == s_rxR)
    {
      s_stats.rxOverruns++;                     /* 环形缓冲满：本帧丢弃并统计 */
    }
    else
    {
      CAN_RxMsg *slot = &s_rxRing[w];

      slot->id        = rxHeader.Identifier;
      slot->timestamp = rxHeader.RxTimestamp;
      /* V1.2.3 的 DataLength 为原始 DLC：经典帧 DLC>8 时有效字节仍为 8 */
      slot->len       = (rxHeader.DataLength > 8U) ? 8U : (uint8_t)rxHeader.DataLength;
      for (uint8_t i = 0U; i < slot->len; i++)
      {
        slot->data[i] = rxData[i];
      }
      __DMB();                                 /* 先写数据后发布索引 */
      s_rxW = next;
    }
    s_stats.rxCount++;
  }

  /* 排空后最后一次 GetRxMessage 会返回 FIFO_EMPTY 并粘滞置位 ErrorCode，
   * 主动清除，否则协议错误计数会虚增 */
  hfdcan->ErrorCode = HAL_FDCAN_ERROR_NONE;
}

/**
  * @brief  轮询排空 Tx Event FIFO（主循环上下文，非中断）
  * @note   【2026-10-12】替代原 HAL_FDCAN_TxEventFifoCallback 的中断路径。
  */
static void CAN_PollTxEvent(void)
{
  FDCAN_HandleTypeDef *hfdcan = &hfdcan2;
  FDCAN_TxEventFifoTypeDef txEvent;
  uint32_t              guard;

  if ((hfdcan->Instance->IR & FDCAN_IT_TX_EVT_FIFO_NEW_DATA) == 0U)
  {
    if ((hfdcan->Instance->IR & FDCAN_IT_TX_EVT_FIFO_ELT_LOST) != 0U)
    {
      __HAL_FDCAN_CLEAR_FLAG(hfdcan, FDCAN_FLAG_TX_EVT_FIFO_ELT_LOST);
      s_stats.txEvtLost++;
    }
    return;
  }

  __HAL_FDCAN_CLEAR_FLAG(hfdcan, FDCAN_FLAG_TX_EVT_FIFO_NEW_DATA);

  guard = 0U;
  while ((guard++ < 16U) &&
         (HAL_FDCAN_GetTxEvent(hfdcan, &txEvent) == HAL_OK))
  {
    uint32_t latency = HAL_GetTick() - s_txTick[txEvent.MessageMarker];

    s_stats.txAckCount++;
    if (latency > s_stats.txMaxLatencyMs)
    {
      s_stats.txMaxLatencyMs = latency;         /* 入队->总线确认最大延迟 */
    }

    s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MIN_MS;  /* 总线健康：复位退避 */
  }

  hfdcan->ErrorCode = HAL_FDCAN_ERROR_NONE;
}

/**
  * @brief  轮询总线错误状态（主循环上下文，非中断）
  * @note   【2026-10-12】替代原 HAL_FDCAN_ErrorStatusCallback 的中断路径。
  */
static void CAN_PollErrorStatus(void)
{
  FDCAN_HandleTypeDef *hfdcan = &hfdcan2;
  uint32_t              ir    = hfdcan->Instance->IR;

  if ((ir & FDCAN_IT_ERROR_PASSIVE) != 0U)
  {
    __HAL_FDCAN_CLEAR_FLAG(hfdcan, FDCAN_FLAG_ERROR_PASSIVE);
    if ((s_busOffBackoffMs < CAN_BUSOFF_BACKOFF_MIN_MS) || (s_busOffBackoffMs == 0U))
    {
      s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MIN_MS;
    }
  }

  if ((ir & FDCAN_IT_BUS_OFF) != 0U)
  {
    __HAL_FDCAN_CLEAR_FLAG(hfdcan, FDCAN_FLAG_BUS_OFF);
    /* 指数退避：1s -> 2s -> 4s ... -> 30s 封顶，避免故障总线上的重连风暴 */
    if (s_busOffBackoffMs == 0U)
    {
      s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MIN_MS;
    }
    else if (s_busOffBackoffMs < CAN_BUSOFF_BACKOFF_MAX_MS)
    {
      s_busOffBackoffMs *= 2U;
      if (s_busOffBackoffMs > CAN_BUSOFF_BACKOFF_MAX_MS)
      {
        s_busOffBackoffMs = CAN_BUSOFF_BACKOFF_MAX_MS;
      }
    }
    /* 记录到期时刻，由 CAN_Task 执行 Stop->Start 恢复 */
    s_busOffDueTick = HAL_GetTick() + s_busOffBackoffMs;
  }

  if ((ir & FDCAN_IT_ERROR_WARNING) != 0U)
  {
    __HAL_FDCAN_CLEAR_FLAG(hfdcan, FDCAN_FLAG_ERROR_WARNING);
  }
}

void CAN_Task(void)
{
  /* ---- 轮询路径（替代原 FDCAN2_IT0 中断）---- */
  CAN_PollRxFifo0();
  CAN_PollTxEvent();
  CAN_PollErrorStatus();

  /* ---- 发送停滞看门狗 ----
   * 判据：Tx FIFO 长时间无空闲空间。正常发送时 3 个缓冲在几十微秒内就排空，
   * 连续 500ms 满 = 帧根本发不出去（对端不 ACK / 位时序不匹配 / 总线断开），
   * 此时必须主动 Abort，否则错误被动状态永远转不成 bus-off，也就永远不恢复。 */
  if (HAL_FDCAN_GetTxFifoFreeLevel(&hfdcan2) == 0U)
  {
    uint32_t nowMs = HAL_GetTick();

    if (s_txStallSince == 0U)
    {
      s_txStallSince = nowMs;
    }
    else if ((nowMs - s_txStallSince) > CAN_TX_STALL_MS)
    {
      s_txStallSince = 0U;
      g_canTxStallAborts++;
      (void)HAL_FDCAN_AbortTxRequest(&hfdcan2,
                                     FDCAN_TX_BUFFER0 | FDCAN_TX_BUFFER1 | FDCAN_TX_BUFFER2);
    }
  }
  else
  {
    s_txStallSince = 0U;                       /* 有空间即视为健康，重置计时 */
  }

  /* ---- bus-off 退避恢复 ---- */
  if ((s_busOffDueTick != 0U) && ((int32_t)(HAL_GetTick() - s_busOffDueTick) >= 0))
  {
    s_busOffDueTick = 0U;

    if (HAL_FDCAN_Stop(&hfdcan2) == HAL_OK)
    {
      (void)HAL_FDCAN_Start(&hfdcan2);
      /* 【2026-10-12】重启后不再挂中断，保持纯轮询 */
      hfdcan2.ErrorCode = HAL_FDCAN_ERROR_NONE;
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

  {
    HAL_StatusTypeDef st = HAL_FDCAN_AddMessageToTxFifoQ(&hfdcan2, &txHeader, txData);

    if (st != HAL_OK)
    {
      s_stats.txErrors++;
      return HAL_ERROR;
    }
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
    uint32_t guard = 0U;                            /* 【2026-10-11】中断内硬上限：
                                                     * 若 RXF0S/RXF0A 推进异常导致
                                                     * GetRxMessage() 恒返回 OK，
                                                     * 此处会变成中断内死循环 ->
                                                     * 主循环无法喂狗 -> IWDG 复位。
                                                     * FIFO 深度仅 3，取 16 次足够排空。 */
    /* 循环排空FIFO，避免高负载下滞留 */
    while ((guard++ < 16U) &&
           (HAL_FDCAN_GetRxMessage(hfdcan, FDCAN_RX_FIFO0, &rxHeader, rxData) == HAL_OK))
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
    uint32_t guard = 0U;                        /* 【2026-10-11】与 Rx 排空同理：
                                                 * GetTxEvent 若因 FIFO 索引异常恒返回
                                                 * HAL_OK，此处会变成中断内死循环 ->
                                                 * 主循环饿死 -> IWDG 复位。TEF 深度仅 3，
                                                 * 取 16 次足够。 */
    while ((guard++ < 16U) &&
           (HAL_FDCAN_GetTxEvent(hfdcan, &txEvent) == HAL_OK))
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
