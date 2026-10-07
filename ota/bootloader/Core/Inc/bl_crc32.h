/**
  ******************************************************************************
  * @file    bl_crc32.h
  * @brief   软件 CRC32（CRC-32/ISO-HDLC，与 zlib.crc32 一致）
  *
  * 契约（ota_common.h）：多项式反射写 0xEDB88320、初值 0xFFFFFFFF、
  * 结果异或 0xFFFFFFFF；自检向量 CRC32("123456789") == 0xCBF43926。
  ******************************************************************************
  */
#ifndef __BL_CRC32_H
#define __BL_CRC32_H

#include "bl_main.h"

/* 流式接口：Init -> Update... -> Final（用于 0x36 接收过程中累计 CRC） */
uint32_t BL_Crc32Init(void);
uint32_t BL_Crc32Update(uint32_t crc, const uint8_t *data, uint32_t len);
uint32_t BL_Crc32Final(uint32_t crc);

/* 一次算完 */
uint32_t BL_Crc32(const uint8_t *data, uint32_t len);

/* 自检：契约向量 + 两个业界公开向量；main() 在任何 Flash 操作之前调用 */
bool BL_Crc32SelfTest(void);

#endif /* __BL_CRC32_H */
