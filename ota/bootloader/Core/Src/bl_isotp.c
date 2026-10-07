/**
  ******************************************************************************
  * @file    bl_isotp.c
  * @brief   BL 自带 ISO-TP 传输层实现（经典 CAN 8 字节帧，单通道半双工）
  *
  * 与既有工程 isotp.c 保持一致的字节级约定（PC 上位机与 APP 侧诊断栈无需为 BL 适配）：
  *   SF  [0x0L, data...]        L = 1..7
  *   FF  [0x1H, LLL, data0..5]  12 位长度；**FF 后首个 CF 的 SN = 1**
  *   CF  [0x2N, data...]        N = SN，0..15 循环
  *   FC  [0x3FS, BS, STmin]     FS:0=CTS 1=WAIT 2=OVFLW
  * 参数：收全 FF 后固定回 FC(CTS, BS=0, STmin=2ms)；N_Bs / N_Cr 超时 1000ms。
  *   STmin=2ms 即"给对端重整备时间"的手段：对端按 2ms 间隔发 CF，BL 有充足时间
  *   把每个 CF 落入 2KB 页缓冲，不会因接收过载丢帧。
  *
  * 半双工：本端发送事务进行中（FF 等 FC / 正在发 CF）时忽略新的接收事务
  * （SF/FF/CF 丢弃并计数 s_halfDuplexDrops）；FC 帧任何时刻都处理，否则发送死锁。
  *
  * 发送方向由主循环 BL_IsoTpTask() 按 STmin 节拍驱动，不用中断/阻塞等待。
  ******************************************************************************
  */
#include "bl_isotp.h"
#include "bl_can.h"
#include <string.h>

/*--------------------------------------- 模块内部宏 --------------------------------------*/
#define BL_ISOTP_FRAME_LEN     8U    /* 经典 CAN 帧固定 8 字节（与既有工程一致） */

/*--------------------------------------- 内部类型 ----------------------------------------*/
typedef enum
{
  BL_ISOTP_RX_IDLE = 0,
  BL_ISOTP_RX_COLLECT
} BL_IsoTpRxState;

typedef enum
{
  BL_ISOTP_TX_IDLE = 0,
  BL_ISOTP_TX_WAIT_FC,
  BL_ISOTP_TX_SENDING
} BL_IsoTpTxState;

/*--------------------------------------- 模块内部变量 ------------------------------------*/
static uint32_t           s_rxId;             /* 本节点接收 ID（对端发送 ID） */
static uint32_t           s_txId;             /* 本节点发送 ID */
static BL_IsoTpRxCallback s_rxCb;

/* 接收方向 */
static BL_IsoTpRxState s_rxState;
static uint8_t         s_rxBuf[BL_ISOTP_MAX_MSG];
static uint16_t        s_rxExpect;            /* FF 声明的总长度 */
static uint16_t        s_rxGot;               /* 已收字节数 */
static uint8_t         s_rxNextSn;            /* 期望的下一个 SN */
static uint32_t        s_rxLastTick;          /* N_Cr 计时 */
static uint32_t        s_rxMsgCount;          /* 完整报文计数（启动窗口判定用） */

/* 发送方向 */
static BL_IsoTpTxState s_txState;
static uint8_t         s_txBuf[BL_ISOTP_MAX_MSG];
static uint16_t        s_txLen;
static uint16_t        s_txSent;              /* 已确认发出的应用字节数 */
static uint8_t         s_txNextSn;
static uint8_t         s_txBlockLeft;         /* 当前块剩余 CF 数（0=不限块） */
static uint16_t        s_txStMinMs;           /* 对端要求的 CF 间隔 */
static uint32_t        s_txLastTick;

/* 诊断计数（调试器可观察；所有异常都计数，不静默吞掉） */
static uint32_t        s_halfDuplexDrops;     /* 半双工期间被忽略的接收帧数 */
static uint32_t        s_snErrors;            /* CF 序号错序次数 */
static uint32_t        s_timeouts;            /* N_Bs / N_Cr 超时次数 */
static uint32_t        s_txFifoBusy;          /* CAN Tx FIFO 满导致的发送失败次数 */
static uint32_t        s_ovflwCount;          /* 对端 FC=OVFLW 次数 */
static uint32_t        s_fcUnexpected;         /* 【M11】非 WAIT_FC 状态收到的 FC 帧数 */

/**
  * @brief  发送 3 字节 FC 流控帧（不关注结果：FC 丢失由对端超时兜底）
  */
static void BL_IsoTpSendFc(uint8_t fs, uint8_t bs, uint8_t stmin)
{
  uint8_t frame[3];

  frame[0] = fs;
  frame[1] = bs;
  frame[2] = stmin;
  (void)BL_CanSend(s_txId, frame, 3U);
}

/**
  * @brief  投递一条完整应用报文：先复位接收状态再回调（回调内可安全发起发送）
  */
static void BL_IsoTpDeliver(const uint8_t *data, uint16_t len)
{
  s_rxState = BL_ISOTP_RX_IDLE;
  s_rxMsgCount++;

  if (s_rxCb != NULL)
  {
    s_rxCb(data, len);
  }
}

/**
  * @brief  处理 FC 帧（对端对我方多帧发送的流控）
  */
static void BL_IsoTpHandleFc(const uint8_t *data)
{
  uint8_t fs;

  if (s_txState != BL_ISOTP_TX_WAIT_FC)
  {
    /* 【M11 修复】原实现直接 return 静默丢弃，与本文件顶部"所有异常都计数，
     * 不静默吞掉"的自我声明相矛盾 —— 这是唯一的漏网点。
     * 现场影响：对端在总线上残留的 FC（多帧交互结束后到达、或双向同时发送）
     * 会让本计数器本可用于现场排查的信号消失，表现为"偶发 ISO-TP 超时"却
     * 无从判断究竟是丢了 CF、还是多了 FC、还是超时参数不对。
     * 计数不改变行为（仍然丢弃，这是协议要求的），只是让异常可观测。*/
    s_fcUnexpected++;
    return;
  }

  fs = (uint8_t)(data[0] & 0x0FU);
  if (fs == 0x0U)                             /* CTS：可以发送 */
  {
    s_txBlockLeft = data[1];                  /* BS=0 表示不限块 */
    if ((data[2] >= 0xF1U) && (data[2] <= 0xF9U))
    {
      s_txStMinMs = 1U;                       /* 100µs 单位：本实现最小按 1ms 处理 */
    }
    else if (data[2] <= 0x7FU)
    {
      s_txStMinMs = data[2];
    }
    else
    {
      s_txStMinMs = 7U;                       /* 0x80~0xF0 保留值：按 7ms 保守处理 */
    }
    s_txState    = BL_ISOTP_TX_SENDING;
    s_txLastTick = HAL_GetTick();
  }
  else if (fs == 0x1U)                        /* Wait：刷新 N_Bs 继续等 */
  {
    s_txLastTick = HAL_GetTick();
  }
  else                                        /* OVFLW：对端缓冲不足，放弃本次发送 */
  {
    s_txState = BL_ISOTP_TX_IDLE;
    s_ovflwCount++;
  }
}

/**
  * @brief  接收方向：处理一帧（ID 已匹配）
  */
static void BL_IsoTpRxFrame(const uint8_t *data, uint8_t len)
{
  uint8_t pci = (uint8_t)(data[0] >> 4U);

  if (pci == 0x3U)                            /* FC 任何时刻都要处理，否则发送方向死锁 */
  {
    BL_IsoTpHandleFc(data);
    return;
  }

  /* 半双工：本端正在发送时不接收新的接收事务 */
  if (s_txState != BL_ISOTP_TX_IDLE)
  {
    s_halfDuplexDrops++;
    return;
  }

  if (pci == 0x0U)                            /* SF：单帧 */
  {
    uint8_t sfLen = (uint8_t)(data[0] & 0x0FU);

    if ((sfLen >= 1U) && (sfLen <= 7U) && (sfLen <= (uint8_t)(len - 1U)))
    {
      BL_IsoTpDeliver(&data[1], (uint16_t)sfLen);
    }
    /* 长度非法的 SF：丢弃（不改变接收状态） */
  }
  else if (pci == 0x1U)                       /* FF：首帧 */
  {
    uint16_t total = (uint16_t)(((uint16_t)(data[0] & 0x0FU) << 8) | (uint16_t)data[1]);
    uint8_t  i;

    if ((total <= 7U) || (total > BL_ISOTP_MAX_MSG))
    {
      BL_IsoTpSendFc(0x32U, 0x00U, 0x00U);    /* 非法长度/超本机缓冲：回 FC(OVFLW) */
      s_rxState = BL_ISOTP_RX_IDLE;
      return;
    }

    s_rxExpect = total;
    s_rxGot    = 6U;                          /* FF 自带前 6 字节 */
    for (i = 0U; i < 6U; i++)
    {
      s_rxBuf[i] = data[2U + i];
    }
    s_rxNextSn   = 1U;                        /* 关键约定：FF 后首个 CF 的 SN = 1 */
    s_rxState    = BL_ISOTP_RX_COLLECT;
    s_rxLastTick = HAL_GetTick();

    /* FC：FS=CTS, BS=0（不限块）, STmin=2ms */
    BL_IsoTpSendFc(0x30U, 0x00U, (uint8_t)BL_ISOTP_STMIN_MS);
  }
  else if (pci == 0x2U)                       /* CF：连续帧 */
  {
    uint8_t sn;
    uint8_t i;

    if (s_rxState != BL_ISOTP_RX_COLLECT)
    {
      return;                                 /* 无接收事务：丢弃 */
    }

    sn = (uint8_t)(data[0] & 0x0FU);
    if (sn != s_rxNextSn)
    {
      s_rxState = BL_ISOTP_RX_IDLE;
      s_snErrors++;                           /* 序号错序：复位接收，等对端重发 */
      return;
    }
    s_rxNextSn = (uint8_t)((s_rxNextSn + 1U) & 0x0FU);

    for (i = 1U; (i < len) && (s_rxGot < s_rxExpect); i++)
    {
      s_rxBuf[s_rxGot] = data[i];
      s_rxGot++;
    }
    s_rxLastTick = HAL_GetTick();

    if (s_rxGot >= s_rxExpect)
    {
      BL_IsoTpDeliver(s_rxBuf, s_rxExpect);
    }
  }
  else
  {
    /* 0x4~0xF 保留 PCI：忽略 */
  }
}

/**
  * @brief  初始化 ISO-TP 通道
  */
void BL_IsoTpInit(uint32_t rxId, uint32_t txId, BL_IsoTpRxCallback rxCb)
{
  s_rxId  = rxId;
  s_txId  = txId;
  s_rxCb  = rxCb;

  s_rxState    = BL_ISOTP_RX_IDLE;
  s_rxExpect   = 0U;
  s_rxGot      = 0U;
  s_rxNextSn   = 1U;
  s_rxLastTick = HAL_GetTick();
  s_rxMsgCount = 0U;

  s_txState     = BL_ISOTP_TX_IDLE;
  s_txLen       = 0U;
  s_txSent      = 0U;
  s_txNextSn    = 1U;
  s_txBlockLeft = 0U;
  s_txStMinMs   = 0U;
  s_txLastTick  = HAL_GetTick();
}

/**
  * @brief  喂入一帧 CAN 报文（ID 不匹配或长度非法则忽略）
  */
void BL_IsoTpOnCanFrame(uint32_t id, const uint8_t *data, uint8_t len)
{
  if ((id != s_rxId) || (data == NULL) || (len < 1U) || (len > BL_ISOTP_FRAME_LEN))
  {
    return;
  }

  BL_IsoTpRxFrame(data, len);
}

/**
  * @brief  发送一条应用报文（<=7 字节走 SF 立即发出；否则 FF 后等对端 FC）
  */
BL_IsoTpResult BL_IsoTpSend(const uint8_t *data, uint16_t len)
{
  uint8_t frame[BL_ISOTP_FRAME_LEN];
  uint8_t i;

  if ((data == NULL) || (len == 0U) || (len > BL_ISOTP_MAX_MSG))
  {
    return BL_ISOTP_TOOLONG;
  }
  if (s_txState != BL_ISOTP_TX_IDLE)
  {
    return BL_ISOTP_BUSY;                     /* 单通道半双工：同时只允许一个发送事务 */
  }

  if (len <= 7U)                              /* 单帧：立即发出 */
  {
    frame[0] = (uint8_t)len;                  /* PCI = 0x0L */
    for (i = 0U; i < (uint8_t)len; i++)
    {
      frame[1U + i] = data[i];
    }
    (void)memset(&frame[1U + len], 0, (size_t)(7U - len));

    if (BL_CanSend(s_txId, frame, BL_ISOTP_FRAME_LEN) != HAL_OK)
    {
      s_txFifoBusy++;
      return BL_ISOTP_BUSY;
    }
    return BL_ISOTP_OK;
  }

  /* 多帧：FF 携带前 6 字节，随后等对端 FC */
  (void)memcpy(s_txBuf, data, (size_t)len);
  s_txLen    = len;
  s_txSent   = 6U;
  s_txNextSn = 1U;                            /* FF 后首个 CF 的 SN = 1 */

  frame[0] = (uint8_t)(0x10U | (uint8_t)((len >> 8) & 0x0FU));
  frame[1] = (uint8_t)(len & 0xFFU);
  for (i = 0U; i < 6U; i++)
  {
    frame[2U + i] = data[i];
  }

  if (BL_CanSend(s_txId, frame, BL_ISOTP_FRAME_LEN) != HAL_OK)
  {
    s_txFifoBusy++;
    return BL_ISOTP_BUSY;
  }

  s_txState    = BL_ISOTP_TX_WAIT_FC;
  s_txLastTick = HAL_GetTick();
  return BL_ISOTP_OK;
}

/**
  * @brief  主循环轮询：STmin 节拍发 CF + N_Bs / N_Cr 超时
  */
void BL_IsoTpTask(void)
{
  uint32_t now = HAL_GetTick();

  /*------------------------------------ 发送方向 ------------------------------------*/
  if (s_txState == BL_ISOTP_TX_WAIT_FC)
  {
    if ((now - s_txLastTick) > BL_ISOTP_TIMEOUT_MS)
    {
      s_txState = BL_ISOTP_TX_IDLE;           /* N_Bs 超时 */
      s_timeouts++;
    }
  }
  else if (s_txState == BL_ISOTP_TX_SENDING)
  {
    if ((now - s_txLastTick) >= (uint32_t)s_txStMinMs)
    {
      uint8_t  frame[BL_ISOTP_FRAME_LEN];
      uint8_t  n    = 0U;
      uint16_t sent = s_txSent;               /* 先快照，发送成功后才提交（失败不丢帧） */
      uint8_t  sn   = s_txNextSn;

      frame[n] = (uint8_t)(0x20U | sn);
      n++;
      while ((n < BL_ISOTP_FRAME_LEN) && (sent < s_txLen))
      {
        frame[n] = s_txBuf[sent];
        n++;
        sent++;
      }
      (void)memset(&frame[n], 0, (size_t)(BL_ISOTP_FRAME_LEN - n));

      if (BL_CanSend(s_txId, frame, BL_ISOTP_FRAME_LEN) == HAL_OK)
      {
        s_txSent     = sent;
        s_txNextSn   = (uint8_t)((sn + 1U) & 0x0FU);
        s_txLastTick = now;

        if (s_txBlockLeft > 0U)
        {
          s_txBlockLeft--;
          if ((s_txBlockLeft == 0U) && (s_txSent < s_txLen))
          {
            s_txState    = BL_ISOTP_TX_WAIT_FC; /* 块发完，等下一个 FC */
            s_txLastTick = now;
          }
        }

        if (s_txSent >= s_txLen)
        {
          s_txState = BL_ISOTP_TX_IDLE;         /* 发送完成 */
        }
      }
      else
      {
        s_txFifoBusy++;
        s_txLastTick = now;                     /* CAN 忙：下个节拍重试同一 CF */
      }
    }
  }
  else
  {
    /* TX_IDLE：无发送事务 */
  }

  /*------------------------------------ 接收方向 ------------------------------------*/
  if (s_rxState == BL_ISOTP_RX_COLLECT)
  {
    if ((now - s_rxLastTick) > BL_ISOTP_TIMEOUT_MS)
    {
      s_rxState = BL_ISOTP_RX_IDLE;           /* N_Cr 超时 */
      s_timeouts++;
    }
  }
}

/**
  * @brief  已收到的完整应用报文数
  */
uint32_t BL_IsoTpGetRxMsgCount(void)
{
  return s_rxMsgCount;
}

/**
  * @brief  接收事务是否进行中
  */
bool BL_IsoTpRxBusy(void)
{
  return (s_rxState == BL_ISOTP_RX_COLLECT);
}

/**
  * @brief  发送事务是否进行中
  */
bool BL_IsoTpTxBusy(void)
{
  return (s_txState != BL_ISOTP_TX_IDLE);
}
