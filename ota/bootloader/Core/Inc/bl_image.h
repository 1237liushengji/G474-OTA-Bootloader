/**
  ******************************************************************************
  * @file    bl_image.h
  * @brief   镜像头解析与跳转前结构校验
  ******************************************************************************
  */
#ifndef __BL_IMAGE_H
#define __BL_IMAGE_H

#include "bl_main.h"

/* 镜像信息（解析成功后的全部关键字段） */
typedef struct
{
  uint8_t  slot;          /* 0 = Slot A，1 = Slot B */
  uint32_t hdrAddr;       /* 实际找到镜像头的地址（槽基址 + n*OTA_IMG_HDR_SEARCH_STEP） */
  uint32_t appBase;       /* APP 链接基址 = hdrAddr + OTA_IMG_HDR_SIZE */
  uint32_t payloadSize;   /* 净固件字节数 */
  uint32_t payloadCrc;    /* 镜像头声明的净固件 CRC32 */
  uint32_t imgSize;       /* 整镜像大小 = 32 + payloadSize */
  uint32_t loadAddr;      /* 镜像头声明的载入地址 */
  uint32_t version;       /* 0xMMmm0000 */
  uint8_t  major;
  uint8_t  minor;
} BL_ImageInfo;

/* 校验结果（与 OTA_ERR_xxx 一一对应，便于写入 cfg.lastError） */
typedef enum
{
  BL_IMG_OK = 0,
  BL_IMG_ERR_HDR_MAGIC,        /* 魔数错        -> OTA_ERR_HDR_MAGIC */
  BL_IMG_ERR_HDR_CRC,          /* 头部自校验失败 -> OTA_ERR_HDR_CRC */
  BL_IMG_ERR_PAYLOAD_CRC,      /* 净固件 CRC 错  -> OTA_ERR_PAYLOAD_CRC */
  BL_IMG_ERR_SIZE_RANGE,       /* 长度越界      -> OTA_ERR_SIZE_RANGE */
  BL_IMG_ERR_LOAD_ADDR,        /* 载入地址不符   -> OTA_ERR_LOAD_ADDR */
  BL_IMG_ERR_SP,               /* 栈顶指针非法   -> OTA_ERR_SP */
  BL_IMG_ERR_RESET_VEC         /* 复位向量非法   -> OTA_ERR_RESET_VEC */
} BL_ImgResult;

/**
  * @brief  只解析并校验镜像头本身（不校验净固件 CRC / 向量表）
  *         校验项：magic、hdrCrc、payloadSize 范围、imgSize 一致性、loadAddr == 头地址+32
  * @param  slot 0=A，1=B
  * @param  info 输出（可为 NULL）
  */
BL_ImgResult BL_ImageReadHeader(uint8_t slot, BL_ImageInfo *info);

/**
  * @brief  完整跳转前校验：镜像头 + 净固件 CRC32 + **独立的** 栈顶指针与复位向量检查
  * @param  slot 0=A，1=B
  * @param  info 输出（可为 NULL）
  */
BL_ImgResult BL_ImageValidate(uint8_t slot, BL_ImageInfo *info);

/**
  * @brief  BL_ImgResult -> OTA_ERR_xxx（写入 cfg.lastError 现场诊断用）
  */
uint32_t BL_ImageErrToOtaErr(BL_ImgResult result);

/**
  * @brief  合成 32B 镜像头（FF02 激活时由设备侧写入槽首，模式A 约定）
  * @param  hdr         输出缓冲，必须 >= OTA_IMG_HDR_SIZE 字节
  * @param  payloadSize 净固件字节数
  * @param  loadAddr    槽 APP 链接基址
  * @param  payloadCrc  净固件 CRC32（0x37 携带值）
  * @param  major       版本主号（无来源时填 0）
  * @param  minor       版本次号（无来源时填 0）
  * @note   hdrCrc 由本函数对 [0,24) 计算后写入；其余保留字节写 0
  */
void BL_ImageBuildHeader(uint8_t *hdr, uint32_t payloadSize, uint32_t loadAddr,
                         uint32_t payloadCrc, uint8_t major, uint8_t minor);

#endif /* __BL_IMAGE_H */
