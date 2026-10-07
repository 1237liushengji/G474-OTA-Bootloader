/**
  ******************************************************************************
  * @file    bl_diag.h
  * @brief   BL 的 UDS 服务子集 + 下载传输状态机（含 2KB 页缓冲）
  ******************************************************************************
  */
#ifndef __BL_DIAG_H
#define __BL_DIAG_H

#include "bl_main.h"

/**
  * @brief  初始化诊断状态机
  * @param  activeSlot 当前 BL 认定的活动槽（0=A，1=B）—— 下载/擦除目标恒为"另一个槽"
  */
void BL_DiagInit(uint8_t activeSlot);

/**
  * @brief  收到一条完整诊断请求（作为 BL_IsoTpInit 的回调，主循环上下文执行）
  */
void BL_DiagDispatch(const uint8_t *req, uint16_t len);

/**
  * @brief  主循环轮询：S3(5s) 会话超时回默认会话
  */
void BL_DiagTask(void);

/**
  * @brief  0x2E F190 是否收到过"下次进 Bootloader"请求
  * @note   BL 中仅记录（并回 6E F1 90）；该标志的语义（下次复位进 BL）由 APP 侧实现
  */
bool BL_DiagBootRequested(void);

/**
  * @brief  下载事务是否进行中（0x34/FF00 之后、未完成）
  */
bool BL_DiagDownloadActive(void);

#endif /* __BL_DIAG_H */
