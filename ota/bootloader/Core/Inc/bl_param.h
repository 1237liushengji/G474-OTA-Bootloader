/**
  ******************************************************************************
  * @file    bl_param.h
  * @brief   Bootloader 控制区（OTA_Cfg）双副本读写
  ******************************************************************************
  */
#ifndef __BL_PARAM_H
#define __BL_PARAM_H

#include "bl_main.h"

/**
  * @brief  载入配置：遍历 4 个候选（2 个物理页 × 页内 2 份副本），
  *         各自校验 magic == OTA_CFG_MAGIC 且结构 CRC 正确，取 seq 最大者为当前值
  * @param  out 输出当前配置（不可为 NULL）
  * @retval true=找到有效副本；false=全部无效（out 为默认值：IDLE_A / trialLeft=0 / 两槽 size=0）
  */
bool BL_ParamLoad(OTA_Cfg *out);

/**
  * @brief  落盘配置（掉电安全）：写入"最旧的有效副本所在位置"，
  *         绝不擦掉唯一最新副本所在页；seq = 当前 seq + 1；擦页前把同页另一份副本
  *         （以及 PRIMARY 页的 BL 向量表）读入 RAM，擦后先回写再写新值；写完回读校验
  * @param  newCfg 新配置（seq/crc 由本函数填写；不可为 NULL）
  * @retval true=成功；false=失败（已尽力保持旧副本可用，调用方必须记录错误）
  */
bool BL_ParamStore(const OTA_Cfg *newCfg);

/**
  * @brief  当前配置（RAM 镜像，只读；BL_ParamLoad/Store 之后有效）
  */
const OTA_Cfg *BL_ParamGet(void);

/**
  * @brief  由 state 求活动槽
  * @retval 0=A（IDLE_A/TRIAL_A/非法值）；1=B（IDLE_B/TRIAL_B）
  */
uint8_t BL_ParamSlotOfState(uint32_t state);

#endif /* __BL_PARAM_H */
