/**
  ******************************************************************************
  * @file    bl_isotp.h
  * @brief   BL 自带 ISO-TP（ISO 15765-2）传输层：SF/FF/CF/FC，单通道半双工
  ******************************************************************************
  */
#ifndef __BL_ISOTP_H
#define __BL_ISOTP_H

#include "bl_main.h"

/* 收到一条完整应用报文时的回调（在主循环上下文执行，可直接发应答） */
typedef void (*BL_IsoTpRxCallback)(const uint8_t *data, uint16_t len);

typedef enum
{
  BL_ISOTP_OK = 0,
  BL_ISOTP_BUSY,        /* 发送事务进行中 */
  BL_ISOTP_TOOLONG,     /* 报文超过 BL_ISOTP_MAX_MSG */
  BL_ISOTP_TIMEOUT,     /* N_Bs / N_Cr 超时 */
  BL_ISOTP_SN_ERROR,    /* 连续帧序号错序 */
  BL_ISOTP_OVERFLOW     /* 对端缓冲溢出（FC OVFLW） */
} BL_IsoTpResult;

/**
  * @brief  初始化 ISO-TP 通道
  * @param  rxId  本节点接收的 CAN ID（= 对端发送 ID，BL 为 0x7E0）
  * @param  txId  本节点发送使用的 CAN ID（BL 为 0x7E8）
  * @param  rxCb  收到完整应用报文的回调（不可为 NULL）
  */
void BL_IsoTpInit(uint32_t rxId, uint32_t txId, BL_IsoTpRxCallback rxCb);

/**
  * @brief  喂入一帧 CAN 报文（ID 不匹配则忽略；发送事务进行中丢弃新的接收事务）
  */
void BL_IsoTpOnCanFrame(uint32_t id, const uint8_t *data, uint8_t len);

/**
  * @brief  主循环轮询：STmin 节拍发 CF + N_Bs/N_Cr 超时
  */
void BL_IsoTpTask(void);

/**
  * @brief  发送一条应用报文（<=7 字节走 SF 立即发出；否则 FF 后等对端 FC）
  * @retval BL_ISOTP_OK / BL_ISOTP_BUSY / BL_ISOTP_TOOLONG
  */
BL_IsoTpResult BL_IsoTpSend(const uint8_t *data, uint16_t len);

/**
  * @brief  已收到的完整应用报文数（启动窗口判定"是否收到诊断请求"用）
  */
uint32_t BL_IsoTpGetRxMsgCount(void);

/**
  * @brief  接收事务是否进行中（已收 FF、CF 未收全）
  */
bool BL_IsoTpRxBusy(void);

/**
  * @brief  发送事务是否进行中
  */
bool BL_IsoTpTxBusy(void);

#endif /* __BL_ISOTP_H */
