/**
  ******************************************************************************
  * @file    ota_tunnel.h
  * @brief   串口 ↔ 诊断CAN 隧道：让 PC 通过 USART1 借道本节点收/发 ISO-TP 帧
  *
  * 用途：现场没有 USB-CAN 适配器时，PC 用纯串口即可完成对远端节点的 UDS/OTA 刷写。
  *   板A(本节点) 只需把 PC 发来的一行解析成一帧 CAN 帧发出去，并把收到的诊断帧按行回吐。
  *
  * 行协议（ASCII，\r 结尾，115200 8N1，与既有 console 命令风格一致）：
  *   PC -> 节点 :  tun on            开启隧道   -> 回 "tunnel: on"
  *                 tun off           关闭隧道   -> 回 "tunnel: off"
  *                 tx <id3hex> <n> <2n个hex>   发一帧 -> 回 "TXOK" 或 "TXERR"
  *   节点 -> PC :  RXTX <id3hex> <dlc> <hex...>  隧道开启期间收到的诊断帧
  *
  * 设计约束（沿用既有工程的取舍）：
  *   1. 仅隧道开启时才打印，避免影响正常业务与既有 sniff 模式；
  *   2. 只转发诊断 ID（0x7E0/0x7E8），业务帧 0x321/0x322 不进隧道；
  *   3. 隧道与本地 `diag` 命令共用同一条 ISO-TP 通道（单事务半双工），
  *      因此隧道开启期间不要使用 `diag` 命令（见设计文档 03 第 6 节）。
  ******************************************************************************
  */
#ifndef __OTA_TUNNEL_H
#define __OTA_TUNNEL_H

#include "main.h"
#include "can.h"        /* CAN_RxMsg：本头文件对外接口用到，必须自带依赖，不能指望调用方先包含 */
#include <stdbool.h>
#include <stdint.h>

/* 诊断 ID：与 isotp.h 的 ISOTP_DIAG_REQ_ID/RESP_ID、can.c 的过滤器保持一致 */
#define OTA_TUNNEL_DIAG_ID_LO   0x7E0U
#define OTA_TUNNEL_DIAG_ID_HI   0x7E8U

void OtaTunnel_Init(void);
bool OtaTunnel_IsOn(void);

/**
  * @brief  处理一条控制台命令行（由 console.c 的 Console_Execute 转发未知命令）
  * @param  cmd  命令名（如 "tun" / "tx"）
  * @param  args 参数串（可为 NULL）
  * @retval true=已识别并处理；false=非隧道命令，交回调用方继续分发
  */
bool OtaTunnel_OnLine(const char *cmd, const char *args);

/**
  * @brief  打印一帧诊断报文（仅隧道开启且 ID 属于诊断 ID 时输出）
  * @note   在主循环 App_Process() 取到帧后调用
  */
void OtaTunnel_PrintFrame(const CAN_RxMsg *msg);

#endif /* __OTA_TUNNEL_H */
