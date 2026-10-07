/**
  ******************************************************************************
  * @file    bl_can.h
  * @brief   BL 的 FDCAN2 经典 CAN 驱动（轮询收帧，不依赖中断）
  ******************************************************************************
  */
#ifndef __BL_CAN_H
#define __BL_CAN_H

#include "bl_main.h"

/* 一帧经典 CAN 报文（BL 只需要标准 ID + 8 字节数据） */
typedef struct
{
  uint32_t id;                  /* 标准 ID（11 位） */
  uint8_t  len;                 /* 有效数据长度（0~8） */
  uint8_t  data[8];             /* 数据域 */
} BL_CanFrame;

extern FDCAN_HandleTypeDef hfdcan2;

/**
  * @brief  初始化 FDCAN2：500k 经典 CAN、只收诊断请求 ID 0x7E0、启动
  * @retval HAL_OK / HAL_ERROR（调用方必须处理，不静默）
  * @note   BL 采用**主循环轮询**收帧（BL_CanPoll），因此不使能任何 FDCAN 中断，
  *         也就不需要 FDCAN2_IT0_IRQHandler
  */
HAL_StatusTypeDef BL_CanInit(void);

/**
  * @brief  发送一帧经典 CAN 数据帧（标准 ID）
  * @retval HAL_OK / HAL_BUSY（Tx FIFO 满）/ HAL_ERROR（参数或入队失败）
  */
HAL_StatusTypeDef BL_CanSend(uint32_t stdId, const uint8_t *data, uint8_t len);

/**
  * @brief  轮询取走一帧接收报文
  * @retval true=取到一帧；false=FIFO 为空
  */
bool BL_CanPoll(BL_CanFrame *frame);

/**
  * @brief  Tx FIFO 是否已排空（0x11 复位前确认应答帧已真正发出）
  * @retval true=Tx FIFO 空闲（深度 3 全部可用）
  */
bool BL_CanTxIdle(void);

/**
  * @brief  释放 FDCAN2（跳转 APP 前调用）
  */
void BL_CanDeInit(void);

#endif /* __BL_CAN_H */
