/**
  ******************************************************************************
  * @file    uds.h
  * @brief   UDS（ISO 14229）精简诊断栈：服务器(板B=ECU) + 客户端(板A=诊断仪)
  *
  * 支持的服务（对标driftregion/iso14229，自研精简版）：
  *   0x10 DiagnosticSessionControl  默认/扩展会话（S3 5s超时回默认）
  *   0x22 ReadDataByIdentifier      F000版本 F001运行时间 F002通信统计 F010 LED状态
  *   0x2E WriteDataByIdentifier     F010 LED控制（需扩展会话，否则NRC 0x22）
  *   0x19 ReadDTCInformation        01按状态掩码读故障码（DTC由真实事件合成）
  *   0x3E TesterPresent             在线保持
  *   NRC：0x11服务不支持 0x12子功能不支持 0x31请求越界 0x22条件不满足
  *
  * 应用层钩子（main.c实现）：App_SetLed/App_GetLed——服务器0x2E写LED用。
  ******************************************************************************
  */
#ifndef __UDS_H
#define __UDS_H

#include "main.h"
#include "isotp.h"
#include <stdint.h>

/*--------------------------------------- 会话定义 ---------------------------------------*/
#define UDS_SESSION_DEFAULT      0x01U
#define UDS_SESSION_EXTENDED     0x03U
#define UDS_S3_TIMEOUT_MS        5000U   /* 扩展会话无活动回退默认会话 */

/*--------------------------------------- DID定义 ---------------------------------------*/
#define UDS_DID_VERSION          0xF000U /* ASCII版本串（多帧，验证ISO-TP收发） */
#define UDS_DID_UPTIME           0xF001U /* 运行时间秒（4字节大端） */
#define UDS_DID_STATS            0xF002U /* tx/rx/txAck/err 4x4字节 */
#define UDS_DID_LED              0xF010U /* LED状态/控制 1字节 */

/*--------------------------------------- API ---------------------------------------*/
void Uds_Init(void);                                   /* 按节点角色初始化 */
void Uds_Task(void);                                   /* 主循环轮询（S3计时/客户端超时） */
void Uds_ServerOnMsg(const uint8_t *req, uint16_t len);/* 服务器：处理一条诊断请求 */
void Uds_ClientOnMsg(const uint8_t *rsp, uint16_t len);/* 客户端：打印诊断应答 */
IsoTp_Result Uds_ClientRequest(const uint8_t *req, uint16_t len); /* 客户端：发请求 */

/* 应用层钩子（main.c实现）：状态驱动LED，低电平点亮 */
void    App_SetLed(uint8_t on);
uint8_t App_GetLed(void);

#endif /* __UDS_H */
