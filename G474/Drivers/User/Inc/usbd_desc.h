/**
  ******************************************************************************
  * @file    usbd_desc.h
  * @brief   USB 设备描述符（设备 + 字符串）
  ******************************************************************************
  */
#ifndef __USBD_DESC_H
#define __USBD_DESC_H

#include "usbd_def.h"

/* 描述符集合，供 USBD_Init(pdev, &CDC_Desc, 0) 使用 */
extern USBD_DescriptorsTypeDef CDC_Desc;

#endif /* __USBD_DESC_H */
