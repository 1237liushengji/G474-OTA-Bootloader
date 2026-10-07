/**
  ******************************************************************************
  * @file    bl_jump.h
  * @brief   跳转到 APP
  ******************************************************************************
  */
#ifndef __BL_JUMP_H
#define __BL_JUMP_H

#include "bl_main.h"

/**
  * @brief  跳转到 APP：设置向量表、栈顶指针并跳转到 APP 复位向量
  * @param  appBase APP 链接基址（= 槽基址 + OTA_IMG_HDR_SIZE，**当前 = 0x80，不是 32**；
  *                见血线 12：头结构体 32B 但槽首保留区 0x80，VTOR 必须 0x80 对齐），
  *                已由 BL_ImageValidate 校验
  * @note   调用前必须先通过 BL_ImageValidate（SP/复位向量合法性已独立检查）；
  *         函数正常不返回（APP 若返回则停在死循环，不跑飞）
  */
void BL_JumpToApp(uint32_t appBase);

#endif /* __BL_JUMP_H */
