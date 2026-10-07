/**
  ******************************************************************************
  * @file    ota_agent.h
  * @brief   APP 侧 OTA 代理：在既有 ISO-TP + UDS 栈上实现 0x34/0x36/0x37 刷写服务
  *
  * 设计原则（与工程既有风格一致）：
  *   1. 不新建协议：直接复用 uds.c 的 Uds_ServerOnMsg() 分发点，新增 5 个 case；
  *   2. 不与业务争资源：0x321/0x322 心跳走另一条过滤器与另一条代码路径，OTA 全程不触碰；
  *   3. 目标槽恒为"非活动槽"：正在运行的槽绝不被写入，因此任何时刻都有一份可回滚的旧固件；
  *   4. 页缓冲落盘：2KB 页对齐写，避免同一页被反复擦了又写。
  *
  * 依赖：ota/common/ota_common.h（分区与协议常量）、工程既有 can.h / isotp.h / bsp_uart.h。
  ******************************************************************************
  */
#ifndef __OTA_AGENT_H
#define __OTA_AGENT_H

#include "main.h"
#include "ota_common.h"
#include <stdint.h>
#include <stdbool.h>

/*--------------------------------------- 应用层可调参数 --------------------------------------*/
#define OTA_SELFCHECK_MS       3000U    /* 试运行固件自检窗口：超时未落定则由 BL 回滚 */
#define OTA_PAGE_BUF_SIZE      OTA_FLASH_PAGE_SIZE   /* 2 KB，与 Flash 页一致 */

/*--------------------------------------- API ------------------------------------------------*/
void     Ota_Init(void);        /* main() 初始化段调用：读控制区、识别本槽、置自检待确认 */
void     Ota_Task(void);        /* 主循环调用：自检通过后落定（TRIAL -> IDLE） */
bool     Ota_IsTrialBoot(void); /* 本次是否为未确认的试运行固件 */
uint8_t  Ota_RunningSlot(void); /* 本固件运行在哪个槽：0=Slot A，1=Slot B */

/* UDS 服务入口（由 uds.c 的 Uds_ServerOnMsg() 转发） */
void     Ota_OnRequestDownload(const uint8_t *req, uint16_t len);   /* 0x34 */
void     Ota_OnTransferData(const uint8_t *req, uint16_t len);      /* 0x36 */
void     Ota_OnTransferExit(const uint8_t *req, uint16_t len);      /* 0x37 */
void     Ota_OnRoutineControl(const uint8_t *req, uint16_t len);    /* 0x31 */
void     Ota_OnEcuReset(const uint8_t *req, uint16_t len);          /* 0x11 */
void     Ota_OnWriteBootReq(const uint8_t *req, uint16_t len);      /* 0x2E F190 */

/* 0x22 只读 DID：由 uds.c 的 Uds_Service22() 在 F180~F184 分支调用
 * @retval 写入 resp 的字节数；0 表示该 DID 不归 OTA 管（调用方回 NRC 0x31） */
uint16_t Ota_ReadDid(uint16_t did, uint8_t *resp, uint16_t respMax);

/* 供 console.c 的 "ota" 命令打印状态用 */
void     Ota_PrintStatus(void);

#endif /* __OTA_AGENT_H */
