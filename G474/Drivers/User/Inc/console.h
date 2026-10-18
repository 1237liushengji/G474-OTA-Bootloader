/**
  ******************************************************************************
  * @file    console.h
  * @brief   串口命令行控制台（运行期调测入口：统计/发帧/波特率切换/总线监视）
  *
  * 报文输出格式（总线监视模式，供PC上位机解析）：
  *   RX <毫秒时间戳> <ID十六进制3位> <DLC> <数据十六进制2位xN>
  *   例：RX 12345 321 8 01 A2 03 ...
  ******************************************************************************
  */
#ifndef __CONSOLE_H
#define __CONSOLE_H

#include <stdbool.h>
#include <stdint.h>
#include "can.h"

/* 【2026-10-11 板身份指纹】构建时用 -D APP_BOARD_FP=FP_A1 注入裸 token，
 * 这里 stringify。目的是把「ST-Link SN <-> 芯片 <-> COM 口 <-> 哪版固件」钉死。
 * 定位完成后删掉。 */
#ifndef APP_BOARD_FP
#define APP_BOARD_FP   FP_X0
#endif
#define APP_STR_(x)    #x
#define APP_STR(x)     APP_STR_(x)
#define APP_BOARD_FINGERPRINT   APP_STR(APP_BOARD_FP)

void Console_Init(void);
void Console_Task(void);
bool Console_SniffEnabled(void);
void Console_PrintFrame(const CAN_RxMsg *msg);

/* main.c中的调试计数器（定位sniff下协议分发路径是否执行） */
extern uint32_t g_dbgPoll;                         /* CAN_PollRx取到帧次数 */
extern uint32_t g_dbgHx;                           /* App_HandleRx调用次数 */
extern uint32_t g_dbgCmd;                          /* App_HandleCmd调用次数 */
extern uint32_t g_dbgResp;                         /* App_HandleResp调用次数 */

#endif /* __CONSOLE_H */
