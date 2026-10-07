/**
  ******************************************************************************
  * @file    isotp.c
  * @brief   ISO 15765-2（ISO-TP）传输层精简实现（经典CAN，8字节帧）
  *
  * 状态机：
  *   接收方向： IDLE --SF--> 投递； IDLE --FF--> RX_COLLECT(回FC) --CF...--> 投递
  *   发送方向： IDLE --SF--> 直接发送
  *              IDLE --FF--> TX_WAIT_FC --FC(CTS)--> TX_SENDING --CF块完且BS>0--> TX_WAIT_FC
  *   超时： TX_WAIT_FC超N_Bs复位； RX_COLLECT超N_Cr复位；均经errCb上报。
  *
  * 参考对标：lishen2/isotp-c（平台无关C实现），本文为自研精简版，
  * 状态与参数命名对照ISO 15765-2标准术语。
  ******************************************************************************
  */
#include "isotp.h"
#include "can.h"
#include <string.h>

/*--------------------------------------- 内部类型 --------------------------------------*/
typedef enum { RX_IDLE = 0, RX_COLLECT } RxState;
typedef enum { TX_IDLE = 0, TX_WAIT_FC, TX_SENDING } TxState;

/*--------------------------------------- 模块状态 --------------------------------------*/
static uint32_t s_rxId;                          /* 本节点接收的ISO-TP帧ID（对端TX id） */
static uint32_t s_txId;                          /* 本节点发送ISO-TP帧使用的ID */

static IsoTp_RxCallback s_rxCb;
static IsoTp_ErrCallback s_errCb;

/* 接收方向 */
static RxState   s_rxState;
static uint8_t   s_rxbuf[ISOTP_MAX_MSG];
static uint16_t  s_rxExpect;                     /* FF声明的总长度 */
static uint16_t  s_rxGot;                        /* 已收字节数 */
static uint8_t   s_rxNextSn;                     /* 期望的下一个SN */
static uint32_t  s_rxLastTick;                   /* N_Cr计时 */

/* 发送方向 */
static TxState   s_txState;
static uint8_t   s_txbuf[ISOTP_MAX_MSG];
static uint16_t  s_txLen;
static uint16_t  s_txSent;                       /* 已发送的应用字节数 */
static uint8_t   s_txNextSn;
static uint8_t   s_txBlockLeft;                  /* 当前块剩余CF数（0=无块限制） */
static uint16_t  s_txStMinMs;                    /* 对端要求的CF间隔 */
static uint32_t  s_txLastTick;                   /* STmin节拍/N_Bs计时 */

/**
  * @brief  初始化ISO-TP通道
  * @param  rxId  本节点接收的帧ID（=对端发送ID）
  * @param  txId  本节点发送使用的ID
  */
void IsoTp_Init(uint32_t rxId, uint32_t txId,
                IsoTp_RxCallback rxCb, IsoTp_ErrCallback errCb)
{
  s_rxId  = rxId;
  s_txId  = txId;
  s_rxCb  = rxCb;
  s_errCb = errCb;
  s_rxState = RX_IDLE;
  s_txState = TX_IDLE;
}

/**
  * @brief  发送一个3字节FC流控帧到总线（结果不关注：FC丢失由对端超时兜底）
  */
static void IsoTp_SendFc(uint8_t fs, uint8_t bs, uint8_t stmin)
{
  uint8_t frame[3];
  frame[0] = fs;
  frame[1] = bs;
  frame[2] = stmin;
  (void)CAN_Send(s_txId, frame, 3U);
}

/**
  * @brief  发起一次应用报文发送
  */
IsoTp_Result IsoTp_Send(const uint8_t *data, uint16_t len)
{
  uint8_t frame[CAN_PAYLOAD_LEN];

  if ((data == NULL) || (len == 0U))
  {
    return ISOTP_TOOLONG;
  }
  if (len > ISOTP_MAX_MSG)
  {
    return ISOTP_TOOLONG;
  }
  if (s_txState != TX_IDLE)
  {
    return ISOTP_BUSY;
  }

  if (len <= 7U)
  {
    /* 单帧SF */
    frame[0] = (uint8_t)(0x0U | len);
    for (uint16_t i = 0U; i < len; i++)
    {
      frame[1U + i] = data[i];
    }
    (void)memset(&frame[1U + len], 0, (size_t)(7U - len));
    if (CAN_Send(s_txId, frame, CAN_PAYLOAD_LEN) != HAL_OK)
    {
      return ISOTP_BUSY;                          /* CAN驱动忙（Tx FIFO满） */
    }
    return ISOTP_OK;
  }

  /* 首帧FF */
  memcpy(s_txbuf, data, len);
  s_txLen      = len;
  s_txSent     = 6U;                              /* FF携带前6字节 */
  s_txNextSn   = 1U;                              /* FF后的首个CF SN=1 */
  frame[0]     = (uint8_t)(0x10U | ((len >> 8) & 0x0FU));
  frame[1]     = (uint8_t)(len & 0xFFU);
  for (uint8_t i = 0U; i < 6U; i++)
  {
    frame[2U + i] = data[i];
  }
  if (CAN_Send(s_txId, frame, CAN_PAYLOAD_LEN) != HAL_OK)
  {
    return ISOTP_BUSY;
  }

  s_txState = TX_WAIT_FC;
  s_txLastTick = HAL_GetTick();
  return ISOTP_OK;
}

/**
  * @brief  接收方向：处理一帧（已确认ID匹配）
  */
static void IsoTp_RxFrame(const uint8_t *data, uint8_t len)
{
  uint8_t pci = (uint8_t)(data[0] >> 4U);

  if (pci == 0x0U)                                /* SF */
  {
    uint8_t sfLen = (uint8_t)(data[0] & 0x0FU);
    if ((sfLen >= 1U) && (sfLen <= 7U) && (sfLen <= (uint8_t)(len - 1U)))
    {
      s_rxState = RX_IDLE;                        /* SF中断任何未完成接收 */
      if (s_rxCb != NULL)
      {
        s_rxCb(&data[1], sfLen);
      }
    }
  }
  else if (pci == 0x1U)                           /* FF */
  {
    uint16_t total = (uint16_t)(((uint16_t)(data[0] & 0x0FU) << 8) | data[1]);
    if (total > ISOTP_MAX_MSG)
    {
      /* 超出本机缓冲：回FC溢出，复位 */
      IsoTp_SendFc(0x32U, 0U, 0U);               /* FS=OVFLW */
      s_rxState = RX_IDLE;
      return;
    }

    s_rxExpect = total;
    s_rxGot    = 6U;                              /* FF携带前6字节 */
    for (uint8_t i = 0U; i < 6U; i++)
    {
      s_rxbuf[i] = data[2U + i];
    }
    s_rxNextSn  = 1U;
    s_rxState   = RX_COLLECT;
    s_rxLastTick = HAL_GetTick();

    /* FC: FS=CTS, BS=0(不限块), STmin=2ms */
    IsoTp_SendFc(0x30U, 0x00U, 0x02U);
  }
  else if (pci == 0x2U)                           /* CF */
  {
    if (s_rxState != RX_COLLECT)
    {
      return;                                     /* 无接收事务，丢弃 */
    }

    uint8_t sn = (uint8_t)(data[0] & 0x0FU);
    if (sn != s_rxNextSn)
    {
      s_rxState = RX_IDLE;
      if (s_errCb != NULL)
      {
        s_errCb(ISOTP_SN_ERROR);
      }
      return;
    }
    s_rxNextSn = (uint8_t)((s_rxNextSn + 1U) & 0x0FU);

    for (uint8_t i = 1U; (i < len) && (s_rxGot < s_rxExpect); i++)
    {
      s_rxbuf[s_rxGot++] = data[i];
    }
    s_rxLastTick = HAL_GetTick();

    if (s_rxGot >= s_rxExpect)
    {
      s_rxState = RX_IDLE;
      if (s_rxCb != NULL)
      {
        s_rxCb(s_rxbuf, s_rxExpect);
      }
    }
  }
  else if (pci == 0x3U)                           /* FC（对端对我方多帧发送的流控） */
  {
    if (s_txState != TX_WAIT_FC)
    {
      return;
    }

    uint8_t fs = (uint8_t)(data[0] & 0x0FU);
    if (fs == 0x0U)                               /* CTS：可以发送 */
    {
      s_txBlockLeft = data[1];                    /* BS=0表示不限块 */
      uint8_t stmin = data[2];
      if ((stmin >= 0xF1U) && (stmin <= 0xF9U))   /* 100µs单位→按1ms处理 */
      {
        s_txStMinMs = 1U;
      }
      else if (stmin <= 0x7FU)
      {
        s_txStMinMs = stmin;
      }
      else
      {
        s_txStMinMs = 7U;
      }
      s_txState   = TX_SENDING;
      s_txLastTick = HAL_GetTick();
    }
    else if (fs == 0x1U)                          /* Wait：刷新N_Bs继续等 */
    {
      s_txLastTick = HAL_GetTick();
    }
    else                                          /* OVFLW */
    {
      s_txState = TX_IDLE;
      if (s_errCb != NULL)
      {
        s_errCb(ISOTP_OVERFLOW);
      }
    }
  }
  else
  {
    /* 保留PCI，忽略 */
  }
}

/**
  * @brief  喂入一帧CAN报文（ID不匹配则忽略）
  */
void IsoTp_OnCanFrame(uint32_t id, const uint8_t *data, uint8_t len)
{
  if ((id == s_rxId) && (len >= 1U) && (len <= CAN_PAYLOAD_LEN))
  {
    IsoTp_RxFrame(data, len);
  }
}

/**
  * @brief  主循环轮询：发送STmin节拍 + 各状态超时
  */
void IsoTp_Task(void)
{
  uint32_t now = HAL_GetTick();

  /* 发送方向 */
  if (s_txState == TX_WAIT_FC)
  {
    if ((now - s_txLastTick) > ISOTP_TIMEOUT_MS)  /* N_Bs超时 */
    {
      s_txState = TX_IDLE;
      if (s_errCb != NULL)
      {
        s_errCb(ISOTP_TIMEOUT);
      }
    }
  }
  else if (s_txState == TX_SENDING)
  {
    if ((now - s_txLastTick) >= s_txStMinMs)      /* STmin节拍到 */
    {
      uint8_t frame[CAN_PAYLOAD_LEN];
      uint8_t n = 0U;

      frame[n++] = (uint8_t)(0x20U | s_txNextSn);
      s_txNextSn = (uint8_t)((s_txNextSn + 1U) & 0x0FU);
      while ((n < CAN_PAYLOAD_LEN) && (s_txSent < s_txLen))
      {
        frame[n++] = s_txbuf[s_txSent++];
      }
      (void)memset(&frame[n], 0, (size_t)(CAN_PAYLOAD_LEN - n));

      if (CAN_Send(s_txId, frame, CAN_PAYLOAD_LEN) == HAL_OK)
      {
        s_txLastTick = now;

        if (s_txBlockLeft > 0U)
        {
          s_txBlockLeft--;
          if ((s_txBlockLeft == 0U) && (s_txSent < s_txLen))
          {
            s_txState = TX_WAIT_FC;               /* 块发完，等下一个FC */
            s_txLastTick = now;
          }
        }

        if (s_txSent >= s_txLen)
        {
          s_txState = TX_IDLE;                    /* 发送完成 */
        }
      }
      else
      {
        s_txLastTick = now;                       /* CAN忙：下个节拍重试该CF */
      }
    }
  }
  else
  {
    /* TX_IDLE */
  }

  /* 接收方向超时 N_Cr */
  if (s_rxState == RX_COLLECT)
  {
    if ((now - s_rxLastTick) > ISOTP_TIMEOUT_MS)
    {
      s_rxState = RX_IDLE;
      if (s_errCb != NULL)
      {
        s_errCb(ISOTP_TIMEOUT);
      }
    }
  }
}
