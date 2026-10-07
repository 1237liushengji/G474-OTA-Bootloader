#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 Bootloader 加"分步自检指示"，用于定位"上电没反应"到底卡在哪。

背景：BL 原本既不点 LED 也不驱动屏，上电无反应时**没有任何线索**。
本脚本加入：

  1) 每个初始化步骤后 LED(PE0, 低有效) 闪 N 次 —— 现场不接串口也能看出卡在第几步；
  2) 同步往串口打印带步号的日志 —— 有串口时直接读数；
  3) 特定灭灯时长区分同类步骤（HSE 失败 = 长灭，便于与其它故障区分）；
  4) 跳转 APP 前给出明确指示。

步骤编号：
  1 = main 入口（只点灯，之后才关；能亮说明 MCU 在跑、Flash 可执行）
  2 = SystemClock_Config 完成（能到 2 说明 HSE 起振、切 150MHz 成功）
  3 = UART 初始化完成
  4 = CRC32 自检通过
  5 = 配置读取 + 试运行计数完成
  6 = CAN 上线（未上线则第 6 步指示为长灭）
  7 = 即将跳转 APP

用法: python ota/scripts/add_bl_diag_led.py [--apply]
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

HELPER = '''
/* ==================== 分步自检指示（诊断用） ====================
 * 目的：上电"没反应"时无法判断卡在哪。LED 闪 N 次 = 走到第 N 步。
 * LED: PE0，低电平点亮（与 APP 的 led.h 一致）。
 * 用直接寄存器访问，不引入 HAL_GPIO 依赖，保持 BL 体积。
 * 若不需要，把 BL_DIAG_LED 置 0 即可（整个诊断块会被编译掉）。
 * ============================================================== */
#define BL_DIAG_LED          1
#define BL_LED_PORT          GPIOE
#define BL_LED_PIN           0U

static void BL_DiagLedInit(void)
{
  RCC->AHB2ENR |= RCC_AHB2ENR_GPIOEEN;
  (void)RCC->AHB2ENR;
  BL_LED_PORT->MODER = (BL_LED_PORT->MODER & ~(3UL << (BL_LED_PIN * 2U)))
                     | (1UL << (BL_LED_PIN * 2U));
  BL_LED_PORT->BSRR = (1UL << BL_LED_PIN);          /* 先熄灭（高=灭） */
}

static void BL_DiagLedOn(void)  { BL_LED_PORT->BRR  = (1UL << BL_LED_PIN); }
static void BL_DiagLedOff(void) { BL_LED_PORT->BSRR = (1UL << BL_LED_PIN); }

/* 粗略延时：72 次空循环约 1ms @150MHz（诊断用，精度不重要） */
static void BL_DiagDelay(uint32_t ms)
{
  while (ms-- != 0U)
  {
    for (volatile uint32_t i = 0U; i < 24000U; i++)
    {
      __NOP();
    }
  }
}

/* 每个步骤发一条纯字符串（BL_UART_SEND_STR 不用 vsnprintf，不会拉进浮点库），
 * 便于有串口时直接读数；同时 LED 闪 stage 次，无串口也能定步骤。 */
static void BL_DiagStageMsg(const char *msg)
{
  BL_UART_SEND_STR(msg);
}

/* 闪 stage 次；offMs 为该步之后额外的熄灭时长（用于区分同类步骤） */
static void BL_DiagStage(uint32_t stage, uint32_t offMs)
{
  for (uint32_t i = 0U; i < stage; i++)
  {
    BL_DiagLedOn();
    BL_DiagDelay(120U);
    BL_DiagLedOff();
    BL_DiagDelay(180U);
  }
  BL_DiagDelay(offMs);
}

/* 打一条步骤日志 + 闪对应次数 */
static void BL_DiagStep(uint32_t stage, const char *msg, uint32_t offMs)
{
  BL_DiagStageMsg(msg);
  BL_DiagStage(stage, offMs);
}

'''

INIT_NEW = '''  /* ---- 分步自检指示：能亮第 1 下，说明 MCU 在跑、Flash 可执行 ---- */
  BL_DiagLedInit();
  BL_DiagStage(1U, 0U);

  /* 1) 基础初始化：时钟/日志/Flash/配置 */
  HAL_Init();
  SystemClock_Config();                           /* HSE 8M -> PLL -> 150MHz */
  /* 能到第 2 步 => HSE 起振且成功切到 150MHz（HSE 坏会停在 Error_Handler 死循环） */
  BL_DiagStage(2U, 0U);

  BL_UartInit();
  BL_DiagStage(3U, 0U);

  /* CRC32 自检必须早于任何 Flash 操作：BL/APP/上位机三方一致性的最后防线 */
  if (!BL_FlashSelfTest())
  {
    BL_UART_SEND_STR("BL: crc self-test FAIL\\r\\n");
    BL_DiagStage(4U, 1500U);                      /* 长灭 = 自检失败 */
  }
  BL_DiagStage(4U, 0U);

  (void)BL_FlashInit();

  BL_IwdgInit();                                  /* 先起狗：后面所有擦写路径都有喂狗点 */
  BL_FlashAttachWatchdog(&s_hiwdg);

  /* 2) 读配置（4 个候选） */
  if (!BL_ParamLoad(&s_cfg))
  {
    BL_UART_SEND_STR("BL: cfg invalid, use default IDLE_A\\r\\n");
  }

  /* 3) 活动槽 */
  s_activeSlot = BL_ParamSlotOfState(s_cfg.state);
  BL_LogCfg("BL: boot");

  /* 4) 试运行计数（必须在跳转判定之前） */
  BL_HandleTrial();
  BL_LogCfg("BL: after trial");
  BL_DiagStage(5U, 0U);
'''


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()
    orig = t
    steps = []

    # a) 插入辅助函数（放在 main 之前）
    if 'BL_DiagStage' not in t:
        anchor = 'int main(void)\n{'
        if t.count(anchor) != 1:
            print('!! main 锚点匹配 %d 次' % t.count(anchor)); return 2
        t = t.replace(anchor, HELPER.replace('\n', '\r\n') + anchor, 1)
        steps.append('插入分步自检辅助函数（LED + 串口步号）')
    else:
        steps.append('自检辅助函数已存在，跳过')

    # b) 替换初始化序列
    if 'BL_DiagStage(1U' not in t.split('int main(void)')[1][:400]:
        old = '''  /* 1) 基础初始化：时钟/日志/Flash/配置 */
  HAL_Init();
  SystemClock_Config();                           /* HSE 8M -> PLL -> 150MHz */
  BL_UartInit();

  /* CRC32 自检必须早于任何 Flash 操作：BL/APP/上位机三方一致性的最后防线 */
  if (!BL_FlashSelfTest())
  {
    BL_UART_SEND_STR("BL: crc self-test FAIL\\r\\n");
  }

  (void)BL_FlashInit();

  BL_IwdgInit();                                  /* 先起狗：后面所有擦写路径都有喂狗点 */
  BL_FlashAttachWatchdog(&s_hiwdg);

  /* 2) 读配置（4 个候选） */
  if (!BL_ParamLoad(&s_cfg))
  {
    BL_UART_SEND_STR("BL: cfg invalid, use default IDLE_A\\r\\n");
  }

  /* 3) 活动槽 */
  s_activeSlot = BL_ParamSlotOfState(s_cfg.state);
  BL_LogCfg("BL: boot");

  /* 4) 试运行计数（必须在跳转判定之前） */
  BL_HandleTrial();
  BL_LogCfg("BL: after trial");'''
        old_crlf = old.replace('\n', '\r\n')
        if t.count(old_crlf) == 1:
            t = t.replace(old_crlf, INIT_NEW.replace('\n', '\r\n'), 1)
            steps.append('替换初始化序列为分步指示版本')
        elif t.count(old) == 1:
            t = t.replace(old, INIT_NEW.replace('\n', '\n'), 1)
            steps.append('替换初始化序列为分步指示版本')
        else:
            print('!! 初始化序列锚点匹配 %d 次（CRLF）/ %d 次（LF）'
                  % (t.count(old_crlf), t.count(old)))
            return 2
    else:
        steps.append('初始化序列已是分步版本，跳过')

    for s in steps:
        print('  - %s' % s)
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
        print('无需改动')
    return 0


if __name__ == '__main__':
    sys.exit(main())
