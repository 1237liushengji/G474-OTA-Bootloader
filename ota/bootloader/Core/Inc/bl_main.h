/**
  ******************************************************************************
  * @file    bl_main.h
  * @brief   Bootloader 公共定义：HAL/CMSIS/契约头、BL 私有常量、Error_Handler
  *
  * 唯一真相源：ota/common/ota_common.h（只读，BL 不得修改）。
  * 本文件只承载"契约未定义但 BL 必需"的私有配置，避免污染三方共用的契约头：
  *   - 上电启动窗口时长 OTA_BOOT_WINDOW_MS
  *   - 诊断 ISO-TP 地址对与传输参数（取值与既有工程 isotp.h 完全一致）
  *   - 跳转前结构校验所需的内核 RAM 边界
  * （Lead 已确认这三项作为 BL 私有配置放在此处，不算改契约。）
  ******************************************************************************
  */
#ifndef __BL_MAIN_H
#define __BL_MAIN_H

#include "stm32g4xx_hal.h"
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/* 相对路径引用契约头：BL 是自包含工程，不依赖任何额外 include 路径。
 * 路径推导：本文件在 ota/bootloader/Core/Inc/，向上三级 = 仓库根，
 *           因此 ../../../common/ota_common.h = 仓库根/ota/common/ota_common.h。
 * （曾误写为 ../../common/... —— 那只到 ota/bootloader，解析失败。） */
#include "../../../common/ota_common.h"

/*------------------------------------ BL 私有运行参数 ------------------------------------*/
#define OTA_BOOT_WINDOW_MS        1000U   /* 上电启动窗口：等待诊断请求的最长时间 */
#define BL_DIAG_REQ_ID            0x7E0U  /* 诊断仪 -> 目标节点（BL 接收） */
#define BL_DIAG_RESP_ID           0x7E8U  /* 目标节点 -> 诊断仪（BL 发送） */
#define BL_ISOTP_MAX_MSG          OTA_ISOTP_MAX_MSG   /* ISO-TP 单条应用报文上限（由 ota_common.h 统一推导，三方必须一致） */
#define BL_ISOTP_TIMEOUT_MS       1000U   /* N_Bs / N_Cr 超时 */
#define BL_ISOTP_STMIN_MS         2U      /* BL 回 FC 时要求的连续帧最小间隔 */

/* 契约一致性锁死：BL 的 ISO-TP 缓冲必须与契约值一致，否则会出现
 * "上位机按 1021 字节发、BL 只收 128"的静默溢流失败（回 FC=OVFLW）。 */
#if (BL_ISOTP_MAX_MSG != OTA_ISOTP_MAX_MSG)
#error "BL_ISOTP_MAX_MSG 必须等于契约 OTA_ISOTP_MAX_MSG"
#endif

/*-------------------------------- 内核 RAM 边界（跳转前结构校验用） ----------------------*/
/* 依据 ota_common.h 头部注释：RAM 128KB @ 0x20000000~0x2001FFFF */
#define BL_IRAM_BASE              0x20000000U
#define BL_IRAM_END               0x2001FFFFU
#define BL_IRAM_TOP               0x20020000U  /* ARM 惯例：初始 SP 允许为 RAM 顶端 one-past-end */

/* 初始化致命失败处理（HAL 调用返回值必须检查，不静默失败） */
void Error_Handler(void);

#endif /* __BL_MAIN_H */