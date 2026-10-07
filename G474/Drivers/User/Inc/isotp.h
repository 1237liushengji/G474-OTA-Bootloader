/**
  ******************************************************************************
  * @file    isotp.h
  * @brief   ISO 15765-2（ISO-TP）传输层精简实现（经典CAN，8字节帧）
  *
  * 功能：把最长ISOTP_MAX_MSG字节的应用报文拆装到8字节CAN帧上传输。
  *   帧类型（首字节高4位）：
  *     0x0 SF 单帧      [0x0L, data...]                          L=1..7
  *     0x1 FF 首帧      [0x1H, LLL, data0..5]                    12位长度
  *     0x2 CF 连续帧    [0x2N, data...]                          N=SN 0..15循环，FF后首帧SN=1
  *     0x3 FC 流控帧    [0x3FS, BS, STmin]  FS:0=继续 1=等待 2=溢出
  *   设计要点（企业级）：
  *     1. 完全非阻塞：IsoTp_Task()在主循环中完成STmin节拍发送与超时管理；
  *     2. 单通道半双工语义：同时仅允许一个发送事务（忙则返回BUSY）；
  *     3. 接收方向：收到FF后自动回FC(BS=0,STmin=2ms)；SN错序/超时(N_Cr 1s)
  *        复位接收机并上报错误；长度超缓冲回FC(OVFLW)；
  *     4. 发送方向：FF后等FC(N_Bs 1s)，FS=CTS按对端BS/STmin发送CF块；
  *     5. 模块按初始化的rxId过滤，其余ID直接忽略。
  ******************************************************************************
  */
#ifndef __ISOTP_H
#define __ISOTP_H

#include "main.h"
#include <stdint.h>

/*--------------------------------------- 配置 ---------------------------------------*/
#define ISOTP_MAX_MSG            1024U  /* 应用层最大报文长度 */
/* 2026 吞吐优化：128 -> 1024。单块净荷 = 1024 - 1(SID) - 1(blockSeq) - 1(余量) = 1021 字节，
 * 满槽 208KB 镜像的 0x36 报文数由 1835 降到 209（端到端 ~76s -> 约 64s；
 * 若按原始 224KB 槽计则为 225 块 / 约 9s 传输）。
 * ⚠️ 必须与 ota/common/ota_common.h 的 OTA_ISOTP_MAX_MSG 保持一致（该头文件有 #if 断言）；
 * ⚠️ 同时必须放大 uds.c 里 Uds_Service22() 的应答缓冲，见其注释。 */
#define ISOTP_DIAG_REQ_ID        0x7E0U /* 板A(诊断仪) -> 板B(ECU) 请求 */
#define ISOTP_DIAG_RESP_ID       0x7E8U /* 板B(ECU) -> 板A(诊断仪) 应答 */
#define ISOTP_TIMEOUT_MS         1000U  /* N_Bs/N_Cr 超时 */

/*--------------------------------------- 结果码 ---------------------------------------*/
typedef enum
{
  ISOTP_OK = 0,
  ISOTP_BUSY,          /* 发送事务进行中 */
  ISOTP_TOOLONG,       /* 报文超长 */
  ISOTP_TIMEOUT,       /* 等FC/等CF超时 */
  ISOTP_SN_ERROR,      /* 连续帧序号错序 */
  ISOTP_OVERFLOW,      /* 对端缓冲溢出(FC OVFLW) */
} IsoTp_Result;

/*--------------------------------------- 回调 ---------------------------------------*/
typedef void (*IsoTp_RxCallback)(const uint8_t *data, uint16_t len);  /* 收到完整报文 */
typedef void (*IsoTp_ErrCallback)(IsoTp_Result result);               /* 发送失败上报 */

/*--------------------------------------- API ---------------------------------------*/
void IsoTp_Init(uint32_t rxId, uint32_t txId,
                IsoTp_RxCallback rxCb, IsoTp_ErrCallback errCb);
void IsoTp_Task(void);                                              /* 主循环轮询 */
void IsoTp_OnCanFrame(uint32_t id, const uint8_t *data, uint8_t len);/* 喂入CAN帧 */
IsoTp_Result IsoTp_Send(const uint8_t *data, uint16_t len);          /* 发送应用报文 */

#endif /* __ISOTP_H */
