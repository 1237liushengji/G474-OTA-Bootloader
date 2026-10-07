/**
  ******************************************************************************
  * @file    bl_flash_if.h
  * @brief   Bootloader Flash 接口：擦除/编程/校验/回读 + 写保护不变量
  ******************************************************************************
  */
#ifndef __BL_FLASH_IF_H
#define __BL_FLASH_IF_H

#include "bl_main.h"

/* 结果码（契约 task-2 C 节固定取值，顺序不得调整） */
typedef enum
{
  OTA_FLASH_OK = 0,
  OTA_FLASH_ERR_LOCK,
  OTA_FLASH_ERR_ERASE,
  OTA_FLASH_ERR_PROGRAM,
  OTA_FLASH_ERR_VERIFY,
  OTA_FLASH_ERR_RANGE,
  OTA_FLASH_ERR_PROTECT
} OTA_FlashResult;

/* 初始化：清 Flash 错误标志（不使能任何外设时钟，G4 的 Flash 接口时钟常开） */
bool BL_FlashInit(void);

/* 挂接 IWDG 句柄：擦/写前后必须喂狗（3s 看门狗，单页擦除 20~40ms） */
void BL_FlashAttachWatchdog(IWDG_HandleTypeDef *hiwdg);

/*------------------------------------ 公开接口（受保护） ---------------------------------*/
/* 任何擦除/写入地址 < OTA_SLOT_A_ADDR 一律返回 OTA_FLASH_ERR_PROTECT：
 * 保护 BL 向量表页(0x08000000) + 控制区主份 + BL 代码区 整段 */
OTA_FlashResult BL_FlashEraseRange(uint32_t addr, uint32_t size);
OTA_FlashResult BL_FlashWrite(uint32_t addr, const uint8_t *data, uint32_t len);
OTA_FlashResult BL_FlashVerify(uint32_t addr, const uint8_t *data, uint32_t len);

/* 注：原有 BL_FlashReadWords() 已于 2026-10-07 删除（M4）。
 * 它无边界检查、返回 void 无法报错、且零调用点，详见 bl_flash_if.c 内的删除说明。
 * 需要按字读回 Flash 时直接解引用 *(volatile const uint32_t *)addr，
 * 并由调用方自行保证地址范围（bl_image.c / bl_param.c 即如此，且各有槽/页白名单）。 */

/*-------------------- 受控旁路（仅 bl_param.c 写控制区副本时使用） -----------------------*/
/* allowCfg=true 额外放行两个控制区页（每页 2KB），其余区域规则不变 */
OTA_FlashResult BL_FlashEraseRangeEx(uint32_t addr, uint32_t size, bool allowCfg);
OTA_FlashResult BL_FlashWriteEx(uint32_t addr, const uint8_t *data, uint32_t len, bool allowCfg);

/* CRC32 自检入口（main 在任何 Flash 操作之前调用一次；失败必须串口告警） */
bool BL_FlashSelfTest(void);

#endif /* __BL_FLASH_IF_H */
