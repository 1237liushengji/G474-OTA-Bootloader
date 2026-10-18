/**
  ******************************************************************************
  * @file    ota_client.h
  * @brief   板A侧 OTA 客户端（板内自发起 UDS 下载序列）—— 见 ota_client.c 详述
  ******************************************************************************
  */
#ifndef OTA_CLIENT_H
#define OTA_CLIENT_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 单块传输字节数（受 ISOTP_MAX_MSG 约束，见 isotp.h） */
#define OTA_CLIENT_CHUNK     1008U   /* 8的倍数且 <= UDS_MAXLEN_FIELD(1016) */

/** 0=关闭（默认，无需提供镜像即可编译）；1=开启 */
#ifndef OTA_CLIENT_ENABLE
#define OTA_CLIENT_ENABLE   0
#endif

/** 主循环调用：推进 OTA 状态机（非阻塞） */
void OtaClient_Task(void);

/** 手动启动一次完整 OTA 序列 */
void OtaClient_Start(void);

/** 取统计（req/rsp/nrc 可为 NULL；state 输出当前状态码） */
void OtaClient_GetStats(uint32_t *req, uint32_t *rsp, uint32_t *nrc, uint8_t *state);

/** 由 uds.c 的 Uds_ClientOnMsg() 回调：收到一条完整 UDS 应答 */
void OtaClient_OnResp(const uint8_t *rsp, uint16_t len);

/** 由 uds.c 的 Uds_ClientErr() 回调：ISO-TP 层发送失败（超时/SN 出错/溢出）。
 *  ⚠️ 该回调运行在 CAN 上下文，只置标志位，状态机推进留给 OtaClient_Task。 */
void OtaClient_OnIsoTpErr(uint8_t err);

#if (OTA_CLIENT_ENABLE != 0)
/* SWD 可直接观测的全局计数 */
extern volatile uint32_t g_otaCliReqCnt;
extern volatile uint32_t g_otaCliRspCnt;
extern volatile uint32_t g_otaCliNrcCnt;
extern volatile uint8_t  g_otaCliState;
extern volatile uint8_t  g_otaCliBlockSeq;
extern volatile uint32_t g_otaCliBlocksOk;
extern volatile uint8_t  g_otaCliLastRsp[8];
extern volatile uint32_t g_otaCliLastNrc;
extern volatile uint32_t g_otaCliReqQueued;   /* 因 ISO-TP 忙而挂起等待的次数 */
#endif

#ifdef __cplusplus
}
#endif

#endif /* OTA_CLIENT_H */