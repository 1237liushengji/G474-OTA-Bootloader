#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定向重放 main.c 的 OTA 接线改动（5 处）。

背景：Keil 打开工程时会重写它管理的源文件，曾把 main.c 还原为原始版本。
本脚本幂等：已应用过的锚点会自动跳过，可反复运行。

用法: python tools_fix_main_c.py
"""
import os

from paths import workspace_root
import sys

ROOT = workspace_root()
P = os.path.join(ROOT, "G474", "Core", "Src", "main.c")

EDITS = [
    ("包含 OTA 头",
     '#include "isotp.h"\n#include "uds.h"',
     '#include "isotp.h"\n#include "uds.h"\n'
     '#include "ota_agent.h"   /* OTA：刷写服务与状态管理 */\n'
     '#include "ota_tunnel.h"  /* OTA：串口<->诊断CAN隧道 */'),

    ("hiwdg 去 static（ota_agent.c 需在 Flash 擦写前喂狗）",
     "static IWDG_HandleTypeDef hiwdg;                   /* 独立看门狗：LSI/32=1kHz，重装载3s */",
     "IWDG_HandleTypeDef hiwdg;                          /* 独立看门狗：LSI/32=1kHz，重装载3s\n"
     "                                                    * 非 static：ota_agent.c 在 Flash 擦写前喂狗需访问 */"),

    ("main() 里初始化 OTA 与隧道",
     "  Uds_Init();                                      /* ISO-TP+UDS诊断栈（角色自动分配） */",
     "  Uds_Init();                                      /* ISO-TP+UDS诊断栈（角色自动分配） */\n"
     "  OtaTunnel_Init();                                /* OTA：串口<->诊断CAN隧道（默认关闭） */\n"
     "  Ota_Init();                                      /* OTA：读控制区/识别本槽/置试运行待确认 */"),

    ("主循环增加 Ota_Task()",
     "    Uds_Task();                                    /* UDS：S3会话/客户端超时 */\n"
     "    HAL_IWDG_Refresh(&hiwdg);                      /* 喂狗（主循环健康证明） */",
     "    Uds_Task();                                    /* UDS：S3会话/客户端超时 */\n"
     "    Ota_Task();                                    /* OTA：试运行自检通过后落定 TRIAL->IDLE */\n"
     "    HAL_IWDG_Refresh(&hiwdg);                      /* 喂狗（主循环健康证明） */"),

    ("App_Process 增加隧道帧回吐（须在 App_HandleRx 之前）",
     "    if (Console_SniffEnabled())\n"
     "    {\n"
     "      Console_PrintFrame(&s_rxMsg);\n"
     "    }\n"
     "    App_HandleRx();",
     "    if (Console_SniffEnabled())\n"
     "    {\n"
     "      Console_PrintFrame(&s_rxMsg);\n"
     "    }\n"
     "    OtaTunnel_PrintFrame(&s_rxMsg);                /* 隧道开启时回吐 0x7E0/0x7E8（原始帧） */\n"
     "    App_HandleRx();"),
]


def main():
    raw = open(P, encoding="utf-8").read()
    applied = skipped = 0
    for name, old, new in EDITS:
        if new in raw:
            print("  跳过（已应用）: %s" % name)
            skipped += 1
            continue
        c = raw.count(old)
        if c != 1:
            print("!! 锚点不唯一: %s（匹配 %d 次）" % (name, c))
            return 2
        raw = raw.replace(old, new, 1)
        print("  已应用: %s" % name)
        applied += 1

    if applied:
        with open(P, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(raw)
        print("\nmain.c 已更新：应用 %d 处，跳过 %d 处" % (applied, skipped))
    else:
        print("\nmain.c 无需改动（%d 处均已应用）" % skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
