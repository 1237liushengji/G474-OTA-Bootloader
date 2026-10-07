/**
  ******************************************************************************
  * @file    app_board_cfg.h
  * @brief   板级配置（本工程只保留 LCD 相关部分）
  *
  * 来源：用户产品 "工业数据采集终端" 的 BSP/app_board_cfg.h，
  *       这里只摘出 bsp_lcd.c 需要的最小集合，避免把整套产品配置拖进来。
  *
  * 接线（Genbotter G474 板 + 1.69" ST7789 240x280 LCD 排座，
  *       与厂商 LCD 例程一致）：
  *         LCD_CS  PD11     LCD_SCL PB3     LCD_SDA PB5
  *         LCD_DC  PD12     LCD_BL  PD13
  *
  * ⚠️ 与本工程既有外设无冲突：
  *    FDCAN2 = PB12/PB13    USART1 = PA9/PA10    LED = PE0
  *    说明：PB3 是 SWO、PB4 是 NJTRST，属 JTAG 复用脚；
  *          SWD（PA13/PA14）调试不受影响，仅放弃 SWO 追踪。
  ******************************************************************************
  */
#ifndef __APP_BOARD_CFG_H
#define __APP_BOARD_CFG_H

#include "main.h"

/* LCD 控制器选择：0 = ST7789 240x280（本板），1 = ILI9341，2 = ST7735 */
#define LCD_CTRL_SELECT          0

/* --- LCD 引脚（软件 SPI）--- */
#define LCD_CS_PORT              GPIOD
#define LCD_CS_PIN               GPIO_PIN_11

#define LCD_SCL_PORT             GPIOB
#define LCD_SCL_PIN              GPIO_PIN_3

#define LCD_SDA_PORT             GPIOB
#define LCD_SDA_PIN              GPIO_PIN_5

#define LCD_DC_PORT              GPIOD
#define LCD_DC_PIN               GPIO_PIN_12

#define LCD_BL_PORT              GPIOD
#define LCD_BL_PIN               GPIO_PIN_13

/* 引脚所在端口的时钟使能（CS/DC/BL 在 GPIOD，SCL/SDA 在 GPIOB） */
#define LCD_GPIO_CLK_ENABLE()    do {                    \
                                   __HAL_RCC_GPIOB_CLK_ENABLE(); \
                                   __HAL_RCC_GPIOD_CLK_ENABLE(); \
                                 } while (0)

#endif /* __APP_BOARD_CFG_H */
