#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 STM32CubeProgrammer CLI 直接烧录指定 hex —— 解决"Keil 只能烧工程输出"的问题。

背景：Keil 的 Download 只烧工程输出目录里那个固定名字的产物（G474.hex / BL.hex），
      因此"板A 烧 A 角色、板B 烧 B 角色"在 Keil 里很别扭（要改宏重编译）。
      STM32CubeProgrammer 可以直接烧**任意 hex 文件**，一块板烧一次即可。

用法:
    python ota/scripts/flash.py --list                      列出 ST-Link
    python ota/scripts/flash.py BL                           烧 Bootloader
    python ota/scripts/flash.py A                            烧板A（G474_A.hex）
    python ota/scripts/flash.py B                            烧板B（G474_B.hex）
    python ota/scripts/flash.py bare                         烧裸机诊断固件
    python ota/scripts/flash.py --file <任意.hex>            烧指定文件

每一步都会：
    1) 校验文件存在并打印 sha256（便于确认烧的是哪一份）；
    2) 打印将要执行的完整命令；
    3) 要求输入 YES 确认后才真正烧录。
"""
import argparse
import hashlib
import os
import subprocess
import sys

from paths import workspace_root

ROOT = workspace_root()
CLI = r'D:\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe'
FW = os.path.join(ROOT, 'firmware')

TARGETS = {
    'bl':   ('BL.hex',                 'Bootloader（含 7 步 LED 自检 + 时钟诊断）→ 0x08000000'),
    'a':    ('G474_A.hex',             'APP，角色 CAN_NODE_A（命令发起方）→ Slot A 0x0800C080'),
    'b':    ('G474_B.hex',             'APP，角色 CAN_NODE_B（应答方）    → Slot A 0x0800C080'),
    'bare': ('BL_bare_blink_TEST.hex', '裸机诊断固件（不依赖 HSE，用于判定 MCU 是否在跑）'),
}


def sha16(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()[:16]


def run(args, capture=True):
    return subprocess.run(args, capture_output=capture, text=True)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('target', nargs='?', help='bl | a | b | bare')
    ap.add_argument('--file', help='直接指定 hex 文件路径')
    ap.add_argument('--list', action='store_true', help='列出已连接的 ST-Link')
    ap.add_argument('--yes', action='store_true', help='跳过确认（谨慎使用）')
    ap.add_argument('--sn', help='指定 ST-Link 序列号（多板同时在线时必填；用 --list 查）')
    ap.add_argument('--erase-all', action='store_true',
                    help='先全片擦除（默认不做；全擦会清掉 BL 与配置区）')
    args = ap.parse_args()

    if not os.path.isfile(CLI):
        print('!! 未找到 STM32CubeProgrammer CLI: %s' % CLI)
        return 2

    if args.list:
        print('=== ST-Link 列表 ===')
        r = run([CLI, '--list'])
        print(r.stdout or r.stderr)
        return 0

    if args.file:
        hex_path, desc = args.file, '（自定义文件）'
        if not os.path.isabs(hex_path):
            hex_path = os.path.join(ROOT, hex_path)
    elif args.target:
        key = args.target.lower()
        if key not in TARGETS:
            print('!! 未知目标 %r；可选: %s' % (args.target, ', '.join(TARGETS)))
            return 2
        name, desc = TARGETS[key]
        hex_path = os.path.join(FW, name)
    else:
        print(__doc__)
        return 2

    if not os.path.isfile(hex_path):
        print('!! 文件不存在: %s' % hex_path)
        print('   （先运行 ota/scripts/build_all_firmware.py 产出固件）')
        return 2

    size = os.path.getsize(hex_path)
    print('目标 : %s' % desc)
    print('文件 : %s' % os.path.relpath(hex_path, ROOT))
    print('大小 : %d 字节   sha256:%s' % (size, sha16(hex_path)))
    print()

    # ⚠️ 关键：擦除策略不能一刀切
    #   * 只写不擦（-w，不带 -e）：CubeProgrammer 会按 hex 覆盖的地址范围
    #     自动擦除涉及的页 —— 这正是"BL 与 APP 共存"所需。
    #   * 若用 `-e all` 全片擦除，烧 APP 时会把 BL 一起擦掉（反之亦然），
    #     两块板就都得重烧两个组件，极易出错。
    #   因此一律只用 -w；仅当用户显式要求 --erase-all 才全擦。
    cmd = [CLI, '-c', 'port=SWD', 'mode=UR']
    if args.sn:
        # 【2026-10-09】多板同时在线时必须指定 SN，否则永远连第一块。
        # 用 --list 查 SN：板1 是 "8"，板2 是 "49FF6D068065575312232167"。
        cmd += ['sn=%s' % args.sn]
    if args.erase_all:
        cmd += ['-e', 'all']
    cmd += ['-w', hex_path, '-v', '-rst']

    print('将执行:')
    print('  ' + ' '.join('"%s"' % c if ' ' in c else c for c in cmd))
    print()
    print('  说明: -c port=SWD  mode=UR(复位后运行)')
    if args.sn:
        print('        sn=%-12s 指定探针（多板在线时必填）' % args.sn)
    if args.erase_all:
        print('        -e all       全片擦除（会清掉另一槽与配置区！）')
    else:
        print('        （不指定 -e）写入时自动擦除涉及的页，BL 与 APP 互不影响')
    print('        -w <hex>     写入   -v 校验   -rst 复位运行')
    print()

    if not args.yes:
        ans = input('确认烧录？输入 YES 继续: ').strip()
        if ans != 'YES':
            print('已取消。')
            return 1

    print()
    print('=== 开始烧录 ===')
    r = subprocess.run(cmd, text=True)
    print()
    if r.returncode == 0:
        print('烧录成功。')
        print('  提示：若没有自动运行，请给板子断电再上电。')
    else:
        print('烧录失败（exit=%d）。常见原因：' % r.returncode)
        print('  * 没检测到 ST-Link   -> 检查 USB、运行 --list')
        print('  * 目标被占用         -> 关闭 Keil 的 debug 会话')
        print('  * 连接失败           -> 确认 SWD 接线、板子供电')
    return r.returncode


if __name__ == '__main__':
    sys.exit(main())
