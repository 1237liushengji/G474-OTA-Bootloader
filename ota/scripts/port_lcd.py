#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把用户产品里已验证的 LCD 驱动（bsp_lcd，ST7789 240x280 软 SPI）移植进本工程。

为什么用它而不是参考例程那套：
  * 它就是**这块板 + 这块屏**上跑过的代码（引脚与参考例程完全一致）；
  * 接口天生适合状态屏：LCD_Print(row,col,...) / LCD_PrintField / LCD_ClearRow；
  * 软件 SPI，不占硬件 SPI1（那颗 W25Q 用 PA5/6/7），也不引入 LCD_Buff 那 2KB 中间缓冲。

新增文件：
  G474/Drivers/User/Inc/app_board_cfg.h   （最小化：只放 LCD 引脚宏）
  G474/Drivers/User/Inc/bsp_lcd.h         （从产品复制）
  G474/Drivers/User/Src/bsp_lcd.c         （从产品复制，去掉 bsp_tick.h 依赖）

用法: python ota/scripts/port_lcd.py [--apply]
"""
import os
import re
import shutil
import sys

from paths import workspace_root

ROOT = workspace_root()
SRC_BSP = r'D:\DSH\工业数据采集终端\BSP'
DST_INC = os.path.join(ROOT, 'G474', 'Drivers', 'User', 'Inc')
DST_SRC = os.path.join(ROOT, 'G474', 'Drivers', 'User', 'Src')

BOARD_CFG = '''/**
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
#define LCD_GPIO_CLK_ENABLE()    do {                    \\
                                   __HAL_RCC_GPIOB_CLK_ENABLE(); \\
                                   __HAL_RCC_GPIOD_CLK_ENABLE(); \\
                                 } while (0)

#endif /* __APP_BOARD_CFG_H */
'''


def main():
    apply_ = '--apply' in sys.argv
    print('源: %s' % SRC_BSP)
    print('目标: %s / %s' % (os.path.relpath(DST_INC, ROOT), os.path.relpath(DST_SRC, ROOT)))
    print()

    jobs = [
        (os.path.join(SRC_BSP, 'bsp_lcd.h'), os.path.join(DST_INC, 'bsp_lcd.h'), None),
        (os.path.join(SRC_BSP, 'bsp_lcd.c'), os.path.join(DST_SRC, 'bsp_lcd.c'), 'patch_c'),
    ]
    for src, dst, mode in jobs:
        if not os.path.exists(src):
            print('  !! 源文件不存在: %s' % src)
            return 2
        exist = os.path.exists(dst)
        print('  %s %s' % ('覆盖' if exist else '新增', os.path.relpath(dst, ROOT)))
        if not apply_:
            continue
        if mode == 'patch_c':
            t = open(src, 'rb').read().decode('utf-8', 'replace')
            # 去掉平台层依赖：bsp_tick.h 未被使用
            t = t.replace('#include "bsp_tick.h"\n', '')
            # 统一 CRLF（与同目录其他源文件一致）
            t = t.replace('\r\n', '\n').replace('\n', '\r\n')
            with open(dst, 'w', encoding='utf-8', newline='') as fh:
                fh.write(t)
        else:
            shutil.copy2(src, dst)

    cfg = os.path.join(DST_INC, 'app_board_cfg.h')
    print('  %s %s' % ('覆盖' if os.path.exists(cfg) else '新增', os.path.relpath(cfg, ROOT)))
    if apply_:
        with open(cfg, 'w', encoding='utf-8', newline='') as fh:
            fh.write(BOARD_CFG.replace('\n', '\r\n'))

    if not apply_:
        print()
        print('干跑完成（加 --apply 生效）')
        return 0

    print()
    print('已移植。剩余人工步骤见 ota/docs/07-LCD状态屏移植与验证.md')
    return 0


if __name__ == '__main__':
    sys.exit(main())
