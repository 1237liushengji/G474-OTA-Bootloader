/**
  ******************************************************************************
  * @file    usb_cdc.h
  * @brief   MCU 原生 USB 虚拟串口（CDC-ACM）—— 替代板载 CH340K 控制台通道
  *
  * 背景（详见 docs/15 §十四）：
  *   板上"USB转串口"口的板载 CH340K 存在两个无法用软件解决的问题：
  *     1) CH340K.TXD → MCU.PA10 **走线开路**。判据（无争议）：把 PA10 配成
  *        "输入 + 内部下拉(40kΩ)"后，在 PC 连续发送 23 万字节期间做
  *        150ms 高频采样（≈7.1MHz），**107 万次全部读到低**。
  *        CH340K 的 TXD 是推挽输出、空闲必为高，若连通绝不可能一直被拉低。
  *     2) PC 每次打开/写入该串口都会经 **RTS→NRST** 把整块板子复位
  *        （RCC_CSR 的 PINRSTF 实锤）。
  *
  *   本模块改用**板上第二个 USB-C 口（"USB设备"）**，走 MCU 原生 USB
  *   （PA11=USB_DM / PA12=USB_DP，AF10），彻底绕开 CH340K。
  *   USB 协议本身没有 DTR/RTS 复位电路，因此"一发送就复位"问题一并消失。
  *
  * 实现方式：复用 ST 官方 USB Device Library（Core + CDC 类，已复制到
  *   G474/Middlewares/ST/STM32_USB_Device_Library），本文件只做
  *   "类接口回调 + 环形缓冲 + 对外 API" 的粘合。
  *
  * 端点分配（与 ST 官方 Virtual COM Port 一致，Windows 免驱）：
  *   EP0    控制（64B）
  *   EP1 IN 批量 IN （0x81）设备 → 主机 数据
  *   EP1 OUT 批量 OUT（0x01）主机 → 设备 数据
  *   EP2 IN 中断 IN （0x82）CDC 通知
  ******************************************************************************
  */
/* ⚠️ 注意：ST 库的 usbd_cdc.h 用的包含守卫是 __USB_CDC_H，与本文件原名
 * 完全同名，会导致库头文件被预处理器**整段跳过**（现象：编译报
 * "unknown type name 'USBD_CDC_ItfTypeDef'"）。故必须改用带
 * CONSOLE 前缀的独立守卫名。 */
#ifndef __USB_CONSOLE_CDC_H
#define __USB_CONSOLE_CDC_H

#include "main.h"
#include <stdint.h>

/*------------------------------- 公共接口 ---------------------------------*/

/**
  * @brief  初始化并启动 USB 设备（时钟、GPIO、PCD、类注册、连接主机）
  * @note   必须在 HAL_Init / SystemClock_Config / MX_GPIO_Init 之后调用。
  *         不阻塞、不等待枚举；枚举成功与否由 UsbCdc_IsConfigured() 查询。
  */
void     UsbCdc_Init(void);

/**
  * @brief  周期任务：把发送环形缓冲的数据搬运到 IN 端点
  * @note   必须由主循环**高频**调用；内部不阻塞（不等待主机取走）。
  */
void     UsbCdc_Task(void);

/** @brief 主机已完成 SET_CONFIGURATION（枚举成功，可收发） */
uint8_t  UsbCdc_IsConfigured(void);

/** @brief USB 总线复位次数（主机反复重试枚举时此值会快速增长） */
uint32_t UsbCdc_ResetCount(void);

/** @brief 接收（主机→设备）可用字节数 */
uint16_t UsbCdc_Available(void);

/** @brief 从接收环形缓冲取数据，返回实际取到的字节数 */
uint16_t UsbCdc_Read(uint8_t *buf, uint16_t len);

/** @brief 取走一个字节（供控制台行解析用），返回 -1 表示无数据 */
int16_t  UsbCdc_GetChar(void);

/**
  * @brief  发送数据（设备→主机）
  * @param  buf 源缓冲；len 长度
  * @retval 实际放入发送缓冲的字节数（缓冲满时截断）
  * @note   非阻塞：只写入环形缓冲，由 UsbCdc_Task() 搬运到端点。
  */
uint16_t UsbCdc_Write(const uint8_t *buf, uint16_t len);

/*------------------------------- 诊断计数 ---------------------------------*/
uint32_t UsbCdc_ResetCount(void);      /* USB 总线复位次数 */
uint32_t UsbCdc_RxBytes(void);         /* 累计收到字节（主机→设备） */
uint32_t UsbCdc_TxBytes(void);         /* 累计发出字节（设备→主机） */
uint32_t UsbCdc_TxDropBytes(void);     /* 因发送缓冲满而丢弃的字节 */
uint8_t  UsbCdc_IsConnected(void);     /* PCD 状态为已连接 */

/* 由 usbd_conf.c 的 HAL_PCD_ResetCallback 调用（USB 总线复位）。
 * 用途：复位次数是判断"主机是否在反复重试枚举"的最直接指标。 */
void     UsbCdc_NotifyReset(void);

#endif /* __USB_CONSOLE_CDC_H */
