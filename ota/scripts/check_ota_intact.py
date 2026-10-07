#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OTA 改动完整性检查（防"被外部流程覆盖回上游原版"）。

背景：曾有一个后台作业在整理之后运行了逐文件取回，可能把改过的文件覆盖回 GitHub 原版；
Keil 也多次重写过工程与源文件。因此每次外部流程跑完后都应执行本检查。

用法: python ota/scripts/check_ota_intact.py
"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()

# (文件, 特征串, 说明) —— 特征串只在"我的改动生效"时才存在
CHECKS = [
    ('G474/Drivers/User/Inc/isotp.h',      '1024U',                       'ISOTP_MAX_MSG=1024（吞吐优化）'),
    ('G474/Drivers/User/Src/can.c',        'ISOTP_DIAG_RESP_ID;      /* 0x7E8 */', '第三过滤器 0x7E8'),
    ('G474/Drivers/User/Src/can.c',        '? 1U : 3U',                   'StdFiltersNbr=3'),
    ('G474/Drivers/User/Src/uds.c',        'resp[ISOTP_MAX_MSG]',         '应答缓冲放大'),
    ('G474/Drivers/User/Src/uds.c',        'Ota_OnRequestDownload',       'OTA 五个服务注册'),
    ('G474/Drivers/User/Src/console.c',    'OtaTunnel_OnLine',            'tun/tx/ota 命令'),
    ('G474/Core/Src/main.c',               'IWDG_HandleTypeDef hiwdg;',   'hiwdg 非 static'),
    ('G474/Core/Src/main.c',               'Ota_Task();',                 '主循环 Ota_Task'),
    ('G474/Core/Src/main.c',               'Ota_Init();',                 'Ota_Init 调用'),
    ('G474/Core/Src/main.c',               'OtaTunnel_PrintFrame',        '隧道帧回吐'),
    ('G474/Core/Src/system_stm32g4xx.c',   '#ifndef VECT_TAB_OFFSET',     '偏移由工程宏提供'),
    ('G474/MDK-ARM/G474.uvprojx',          'VECT_TAB_OFFSET=0xC080',      'APP 偏移宏'),
    ('G474/MDK-ARM/G474.uvprojx',          '0x800C020',                   'APP IROM1'),
    ('G474/MDK-ARM/G474.uvprojx',          'ota_agent.c',                 '注册 ota_agent.c'),
    ('G474/Drivers/User/Src/ota_agent.c',  'Ota_CfgBlockWrite',           'ota_agent.c 已落地'),
    ('G474/Drivers/User/Src/ota_tunnel.c', 'OtaTunnel_PrintFrame',        'ota_tunnel.c 已落地'),
    ('ota/common/ota_common.h',            'OTA_ISOTP_MAX_MSG',           '契约头完整'),
    ('ota/bootloader/MDK-ARM/BL.uvprojx',  'VECT_TAB_OFFSET=0x0',         'BL 偏移宏'),
    ('ota/bootloader/MDK-ARM/BL.uvprojx',  'system_stm32g4xx.c',          'BL 含 system 文件'),
]

# 上游文件必须**不存在**的特征（防止被误改回上游语义）
MUST_NOT = [
    ('G474/MDK-ARM/G474.uvprojx', '0x8008020', 'APP IROM 残留旧地址'),
    ('G474/MDK-ARM/G474.uvprojx', '<Size>0x37FE0</Size>', 'APP IROM 残留旧尺寸'),
]


def main():
    bad = 0
    print('=== 必须存在的 OTA 改动 ===')
    for path, pat, name in CHECKS:
        p = os.path.join(ROOT, path.replace('/', os.sep))
        if not os.path.isfile(p):
            print('  [缺失文件] %-30s %s' % (name, path)); bad += 1; continue
        t = open(p, encoding='utf-8', errors='replace').read()
        # 大小写不敏感：Keil 重写工程时会把 0x800C020 变成 0x800c020，不该误报
        if pat.lower() in t.lower():
            print('  [OK] %s' % name)
        else:
            print('  [!! 丢失] %-30s %s  期望包含: %s' % (name, path, pat[:40])); bad += 1

    print()
    print('=== 必须不存在的旧值 ===')
    for path, pat, name in MUST_NOT:
        p = os.path.join(ROOT, path.replace('/', os.sep))
        if not os.path.isfile(p):
            print('  [缺失文件] %s' % name); bad += 1; continue
        t = open(p, encoding='utf-8', errors='replace').read()
        if pat.lower() in t.lower():
            print('  [!! 残留] %-30s %s' % (name, pat)); bad += 1
        else:
            print('  [OK] %s' % name)

    print()
    print('=' * 62)
    print('结论:', 'OTA 改动全部完好' if bad == 0 else '有 %d 处问题，需修复' % bad)
    print('=' * 62)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
