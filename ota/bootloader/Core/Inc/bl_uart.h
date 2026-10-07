/**
  ******************************************************************************
  * @file    bl_uart.h
  * @brief   BL 极简阻塞串口日志（USART1 PA9/PA10，115200 8N1）
  ******************************************************************************
  */
#ifndef __BL_UART_H
#define __BL_UART_H

#include "bl_main.h"

/* 初始化 USART1：PA9=TX/PA10=RX，AF7，115200 8N1，无流控（与既有 bsp_uart.c 一致） */
void BL_UartInit(void);

/* 阻塞发送（带 100ms 超时保护）；len==0 或 data==NULL 时直接返回 */
void BL_UartSend(const char *data, uint16_t len);

/* 便捷宏：发送字符串字面量时长度由编译器计算，避免手数字节数写错（少发/多发/越界读） */
#define BL_UART_SEND_STR(s)   BL_UartSend((s), (uint16_t)(sizeof(s) - 1U))

/* 格式化打印（行缓冲 96 字节，仅主循环调用，无重入） */
void BL_UartPrintf(const char *fmt, ...);

/* 释放 USART1（跳转 APP 前调用，让 APP 从干净状态初始化） */
void BL_UartDeInit(void);

#endif /* __BL_UART_H */
