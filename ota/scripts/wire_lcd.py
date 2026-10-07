#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 LCD 接入 APP：初始化 + 启动画面 + 主循环里 1 秒刷新的测试画面。

关键顺序：LCD 初始化含约 760ms 的 HAL_Delay，**必须放在 IWDG 启动之前**，
否则会被 3 秒看门狗复位。

用法: python ota/scripts/wire_lcd.py [--apply]
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'G474', 'Core', 'Src', 'main.c')

# 1) 头文件（分开判断，避免"bsp_lcd.h 已有"导致 stdio.h 漏插）
INC_LCD_ANCHOR = '#include "ota_tunnel.h"  /* OTA：串口<->诊断CAN隧道 */'
INC_LCD_NEW = INC_LCD_ANCHOR + '\n#include "bsp_lcd.h"      /* LCD：ST7789 240x280 状态屏 */'
INC_STDIO_ANCHOR = '#include "bsp_lcd.h"      /* LCD：ST7789 240x280 状态屏 */'
INC_STDIO_NEW = INC_STDIO_ANCHOR + '\n#include <stdio.h>        /* snprintf：LCD 状态行格式化 */'

# 1b) 工程注册（Keil uvprojx 用 LF 行尾，故用正则匹配任意行尾）
#     替换内容用函数返回，避免 re 把路径里的 \D 当成转义序列
PROJ = os.path.join(ROOT, 'G474', 'MDK-ARM', 'G474.uvprojx')
PROJ_PAT = re.compile(
    r'<FileName>uds\.c</FileName>\s*<FileType>1</FileType>\s*'
    r'<FilePath>\.\.\\Drivers\\User\\Src\\uds\.c</FilePath>\s*</File>')
PROJ_FILE_BLOCK = """
            <File>
              <FileName>bsp_lcd.c</FileName>
              <FileType>1</FileType>
              <FilePath>..\\Drivers\\User\\Src\\bsp_lcd.c</FilePath>
            </File>"""


def _add_bsp_file(m):
    return m.group(0) + PROJ_FILE_BLOCK

# 2) 初始化 + 启动画面（插在 Ota_Init() 之后、IWDG 之前）
INIT_ANCHOR = '  Ota_Init();                                      /* OTA：读控制区/识别本槽/置试运行待确认 */'
INIT_NEW = INIT_ANCHOR + '''

  /* ---- LCD 状态屏（必须早于 IWDG 启动：LCD_Init 内含约 760ms 延时）---- */
  LCD_Init();                                      /* 背光开 + 清屏为黑，失败则全白/花屏 */
  LCD_Print(0, 0, "STM32G474 OTA SYSTEM", LCD_COLOR_CYAN, LCD_COLOR_BLACK);
  LCD_Print(1, 0, "LCD OK / soft-SPI 240x280", LCD_COLOR_GREEN, LCD_COLOR_BLACK);
  LCD_Print(3, 0, "waiting for CAN link...", LCD_COLOR_GRAY, LCD_COLOR_BLACK);'''

# 3) 主循环任务
LOOP_ANCHOR = '    Ota_Task();                                    /* OTA：试运行自检通过后落定 TRIAL->IDLE */'
LOOP_NEW = LOOP_ANCHOR + '\n    LcdUi_Task();                                  /* LCD：1 秒刷新一次状态指征 */'

# 4) LcdUi_Task 实现（放在 App_Process 之前）
TASK_IMPL = '''/**
  * @brief  LCD 测试画面：每秒刷新一次，用来证明"屏在活、且在持续刷新"。
  * @note   刻意不用整屏重绘：软 SPI 全屏约 1M bit，耗时近 1 秒会拖慢主循环。
  *         这里只改动的行（LCD_PrintField 定宽覆盖，短值不留残影）。
  */
static void LcdUi_Task(void)
{
  static uint32_t s_lastLcd = 0U;
  static uint32_t s_boots  = 0U;
  char            line[24];
  uint32_t        now = HAL_GetTick();

  if ((now - s_lastLcd) < 1000U)
  {
    return;
  }
  s_lastLcd = now;
  s_boots++;

  (void)snprintf(line, sizeof(line), "uptime %lus", (unsigned long)(now / 1000U));
  LCD_PrintField(5, 0, line, LCD_CHAR_COLS, LCD_COLOR_WHITE, LCD_COLOR_BLACK);

  (void)snprintf(line, sizeof(line), "tick %lu", (unsigned long)s_boots);
  LCD_PrintField(6, 0, line, LCD_CHAR_COLS, LCD_COLOR_YELLOW, LCD_COLOR_BLACK);

  /* 第 8 行交替 "|" 与空格：即使字库/坐标有问题，也能看出刷新确实在发生 */
  LCD_Print(8, 0, ((s_boots & 1U) != 0U) ? "|" : " ", LCD_COLOR_RED, LCD_COLOR_BLACK);
}

'''


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()
    orig = t
    steps = []

    if 'bsp_lcd.h' not in t:
        if t.count(INC_LCD_ANCHOR) != 1:
            print('!! 头文件锚点匹配 %d 次' % t.count(INC_LCD_ANCHOR)); return 2
        t = t.replace(INC_LCD_ANCHOR, INC_LCD_NEW, 1)
        steps.append('加入 #include "bsp_lcd.h"')
    else:
        steps.append('bsp_lcd.h 已包含，跳过')

    if 'stdio.h' not in t:
        if t.count(INC_STDIO_ANCHOR) != 1:
            print('!! stdio 锚点匹配 %d 次' % t.count(INC_STDIO_ANCHOR)); return 2
        t = t.replace(INC_STDIO_ANCHOR, INC_STDIO_NEW, 1)
        steps.append('加入 #include <stdio.h>（snprintf）')
    else:
        steps.append('stdio.h 已包含，跳过')

    if 'LCD_Init();' not in t:
        if t.count(INIT_ANCHOR) != 1:
            print('!! 初始化锚点匹配 %d 次' % t.count(INIT_ANCHOR)); return 2
        t = t.replace(INIT_ANCHOR, INIT_NEW, 1)
        steps.append('加入 LCD_Init + 启动画面（IWDG 之前）')
    else:
        steps.append('初始化已存在，跳过')

    if 'LcdUi_Task();' not in t:
        if t.count(LOOP_ANCHOR) != 1:
            print('!! 主循环锚点匹配 %d 次' % t.count(LOOP_ANCHOR)); return 2
        t = t.replace(LOOP_ANCHOR, LOOP_NEW, 1)
        steps.append('主循环加入 LcdUi_Task')
    else:
        steps.append('主循环调用已存在，跳过')

    if 'static void LcdUi_Task(void)' not in t:
        fn_anchor = 'static void App_Process(void)\n{'
        if t.count(fn_anchor) != 1:
            print('!! 任务实现锚点匹配 %d 次' % t.count(fn_anchor)); return 2
        t = t.replace(fn_anchor, TASK_IMPL.replace('\n', '\r\n') + fn_anchor, 1)
        steps.append('加入 LcdUi_Task 实现')
    else:
        steps.append('任务实现已存在，跳过')

    for s in steps:
        print('  - %s' % s)

    # ---- 工程注册（独立文件，单独判断与写回）----
    if os.path.isfile(PROJ):
        pt = open(PROJ, encoding='utf-8', errors='replace').read()
        if 'bsp_lcd.c' in pt:
            print('  - 工程已注册 bsp_lcd.c，跳过')
        else:
            pt2, n = PROJ_PAT.subn(_add_bsp_file, pt, count=1)
            if n == 1:
                print('  - 工程注册 bsp_lcd.c')
                if apply_:
                    open(PROJ, 'w', encoding='utf-8', newline='').write(pt2)
            else:
                print('  !! 工程注册锚点匹配 %d 次，需手工添加' % n)

    if t != orig:
        if apply_:
            open(MAIN, 'w', encoding='utf-8', newline='').write(t)
            print()
            print('已写入 %s' % os.path.relpath(MAIN, ROOT))
        else:
            print()
            print('干跑（加 --apply 生效）')
    else:
        print()
        print('main.c 无需改动')
    return 0


if __name__ == '__main__':
    sys.exit(main())
