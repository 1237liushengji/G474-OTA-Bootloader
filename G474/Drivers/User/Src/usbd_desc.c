/**
  ******************************************************************************
  * @file    usbd_desc.c
  * @brief   USB 设备描述符实现（设备描述符 + 字符串描述符）
  *
  * 说明：
  *   - 配置描述符**不在这里** —— ST 的 CDC 类自带（usbd_cdc.c 里的
  *     USBD_CDC_CfgFSDesc），由 usbd_ctlreq.c 通过 pClass->GetFSConfigDescriptor()
  *     取得，所以本文件只提供设备描述符与字符串描述符。
  *   - VID/PID 采用 ST 官方 Virtual COM Port 的 0x0483/0x5740，
  *     Windows 10/11 会直接按标准 CDC-ACM 绑定 usbser.sys，免装驱动。
  ******************************************************************************
  */
#include "usbd_desc.h"
#include "usbd_conf.h"

/*------------------------------------------------------------ 字符串常量 ----*/
#define USBD_VID              0x0483U
#define USBD_PID              0x5740U
#define USBD_LANGID_STRING    0x0409U          /* 英语(美国) */

#define USBD_MANUFACTURER_STRING   "Genbotter"
#define USBD_PRODUCT_STRING_FS     "STM32G474 OTA Console"
#define USBD_SERIALNUMBER_STRING_FS "00000000001A"
#define USBD_CONFIGURATION_STRING_FS "CDC Config"
#define USBD_INTERFACE_STRING_FS     "CDC Interface"

/*---------------------------------------------------------- 描述符缓冲区 ----*/
static uint8_t s_strDesc[USBD_MAX_STR_DESC_SIZ];

/*----------------------------------------------- 设备描述符（18 字节） ------*/
__ALIGN_BEGIN static uint8_t s_devDesc[USB_LEN_DEV_DESC] __ALIGN_END =
{
  USB_LEN_DEV_DESC,              /* bLength                    */
  USB_DESC_TYPE_DEVICE,          /* bDescriptorType            */
  0x00, 0x02,                    /* bcdUSB = 2.00              */
  0x02,                          /* bDeviceClass    = CDC      */
  0x00,                          /* bDeviceSubClass            */
  0x00,                          /* bDeviceProtocol            */
  0x40,                          /* bMaxPacketSize0 = 64       */
  LOBYTE(USBD_VID), HIBYTE(USBD_VID),
  LOBYTE(USBD_PID), HIBYTE(USBD_PID),
  0x00, 0x02,                    /* bcdDevice = 2.00           */
  0x01,                          /* iManufacturer              */
  0x02,                          /* iProduct                   */
  0x03,                          /* iSerialNumber              */
  0x01                           /* bNumConfigurations         */
};

/*-------------------------------------------- 语言 ID 字符串（4 字节） ------*/
__ALIGN_BEGIN static uint8_t s_langIDDesc[USB_LEN_LANGID_STR_DESC] __ALIGN_END =
{
  USB_LEN_LANGID_STR_DESC,
  USB_DESC_TYPE_STRING,
  LOBYTE(USBD_LANGID_STRING),
  HIBYTE(USBD_LANGID_STRING),
};

/*------------------------------------------------------------- 内部函数 ----*/
/**
  * @brief  把 ASCII 字符串转成 USB 字符串描述符（UTF-16LE）
  * @param  desc 输出缓冲（至少 2 + 2*len 字节）
  * @param  str  ASCII 源串
  * @retval 描述符总长度（字节）
  */
static uint16_t USBD_MakeStrDesc(uint8_t *desc, const char *str)
{
  uint16_t i;
  uint16_t len = 0U;

  while ((str[len] != '\0') && (len < 126U))
  {
    len++;
  }

  desc[0] = (uint8_t)(2U + (len * 2U));          /* bLength */
  desc[1] = USB_DESC_TYPE_STRING;                /* bDescriptorType */

  for (i = 0U; i < len; i++)
  {
    desc[2U + (i * 2U)]      = (uint8_t)str[i];  /* 低字节 = ASCII */
    desc[3U + (i * 2U)]      = 0x00U;            /* 高字节 = 0     */
  }

  return (uint16_t)(2U + (len * 2U));
}

/*-------------------------------------------------------- 描述符回调实现 ----*/
static uint8_t *USBD_GetDeviceDescriptor(USBD_SpeedTypeDef speed, uint16_t *length)
{
  (void)speed;
  *length = (uint16_t)USB_LEN_DEV_DESC;
  return s_devDesc;
}

static uint8_t *USBD_GetLangIDStrDescriptor(USBD_SpeedTypeDef speed, uint16_t *length)
{
  (void)speed;
  *length = (uint16_t)USB_LEN_LANGID_STR_DESC;
  return s_langIDDesc;
}

static uint8_t *USBD_GetManufacturerStrDescriptor(USBD_SpeedTypeDef speed, uint16_t *length)
{
  (void)speed;
  *length = USBD_MakeStrDesc(s_strDesc, USBD_MANUFACTURER_STRING);
  return s_strDesc;
}

static uint8_t *USBD_GetProductStrDescriptor(USBD_SpeedTypeDef speed, uint16_t *length)
{
  (void)speed;
  *length = USBD_MakeStrDesc(s_strDesc, USBD_PRODUCT_STRING_FS);
  return s_strDesc;
}

static uint8_t *USBD_GetSerialStrDescriptor(USBD_SpeedTypeDef speed, uint16_t *length)
{
  (void)speed;
  *length = USBD_MakeStrDesc(s_strDesc, USBD_SERIALNUMBER_STRING_FS);
  return s_strDesc;
}

static uint8_t *USBD_GetConfigurationStrDescriptor(USBD_SpeedTypeDef speed, uint16_t *length)
{
  (void)speed;
  *length = USBD_MakeStrDesc(s_strDesc, USBD_CONFIGURATION_STRING_FS);
  return s_strDesc;
}

static uint8_t *USBD_GetInterfaceStrDescriptor(USBD_SpeedTypeDef speed, uint16_t *length)
{
  (void)speed;
  *length = USBD_MakeStrDesc(s_strDesc, USBD_INTERFACE_STRING_FS);
  return s_strDesc;
}

/*-------------------------------------------------------------- 导出集合 ---*/
USBD_DescriptorsTypeDef CDC_Desc =
{
  USBD_GetDeviceDescriptor,
  USBD_GetLangIDStrDescriptor,
  USBD_GetManufacturerStrDescriptor,
  USBD_GetProductStrDescriptor,
  USBD_GetSerialStrDescriptor,
  USBD_GetConfigurationStrDescriptor,
  USBD_GetInterfaceStrDescriptor,
};
