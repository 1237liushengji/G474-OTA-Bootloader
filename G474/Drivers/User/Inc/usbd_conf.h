/**
  ******************************************************************************
  * @file    usbd_conf.h
  * @brief   USB Device Library 配置（供 ST 官方中间件使用）
  *
  * 本工程把 ST 的 USB Device Library（Core + CDC 类）从
  *   STM32H7xx_DFP 包的 Middlewares/ST/STM32_USB_Device_Library 复制到
  *   G474/Middlewares/ST/STM32_USB_Device_Library
  * 该库的 Core/Class 层与具体 USB 内核无关（只通过 USBD_LL_* 接口访问硬件），
  * 因此可直接用于 STM32G474 的经典 USB 设备内核（USB_DRD_FS）。
  ******************************************************************************
  */
#ifndef __USBD_CONF_H
#define __USBD_CONF_H

#include "main.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*------------------------------------------------------------------ 配置项 ---*/
/* CDC 需要两个接口：0 = 通信控制接口，1 = 数据接口 */
#define USBD_MAX_NUM_INTERFACES        2U
#define USBD_MAX_NUM_CONFIGURATION     1U
#define USBD_MAX_STR_DESC_SIZ          0x100U
#define USBD_SUPPORT_USER_STRING_DESC  0U
#define USBD_SELF_POWERED              1U
#define USBD_DEBUG_LEVEL               0U

#define USBD_CDC_INTERVAL              1000U

/*-------------------------------------------------- 内存分配（禁用堆） ------*/
/* 【为什么不用 malloc】嵌入式固件运行期不应依赖动态分配（碎片化 + 不可预测）。
 * CDC 类初始化时只申请一次固定大小的 pClassData，因此用**静态池**满足即可。
 * 池大小取 2KB，远大于 USBD_CDC_HandleTypeDef（约 80~300 字节）。 */
#define USBD_malloc                USBD_static_malloc
#define USBD_free                  USBD_static_free
void *USBD_static_malloc(uint32_t size);
void  USBD_static_free(void *p);

/*---------------------------------------------------------------- 日志 ----*/
#define USBD_UsrLog(...)   do {} while (0)
#define USBD_ErrLog(...)   do {} while (0)
#define USBD_DbgLog(...)   do {} while (0)

#endif /* __USBD_CONF_H */
