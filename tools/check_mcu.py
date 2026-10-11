#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 SWD 判断 MCU "在不在跑、跑到哪"（不依赖串口）。

原理：
  1) hotplug 连接会把内核 halt 在**当前 PC** —— 这个 PC 直接告诉我们卡在哪：
       0x0800xxxx 且 < 0x0800C000 -> 在 BL 里
       0x0800C080 ~ ...           -> 在 APP 里
       0x1FFFxxxx                 -> 在 ROM bootloader
       0xFFFFFFFE                 -> Lockup（双故障）
  2) 读 SysTick->VAL 两次：如果两次不同，说明中断在跑（但 halt 后 SysTick 会停，
     所以此判据仅在"连续两次不 halt"时有效；这里用 PC 为主）。

用法:
    python check_mcu.py --sn 8
"""
import argparse
import re
import subprocess
import time

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"

# Cortex-M4 调试端口寄存器
DHCSR = 0xE000EDF0
DCRSR = 0xE000EDF4
DCRDR = 0xE000EDF8

# 我们直接读寄存器的另一个办法：CubeProgrammer 的 -coreReg
# 但更通用的是用 hotplug 连接后读 RAM 里的固定变量。


def run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def rd32(sn, addr, nbytes=4):
    out = run([CLI, "-c", f"port=SWD sn={sn} mode=hotplug", "-r32", hex(addr), str(nbytes)]).stdout
    vals = []
    for m in re.finditer(r"0x[0-9A-Fa-f]{8}\s*:\s*([0-9A-Fa-f ]+)", out):
        for w in m.group(1).split():
            if len(w) == 8:
                vals.append(int(w, 16))
    return vals, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    args = ap.parse_args()
    sn = args.sn

    print(f"=== 检查 MCU 状态 (SN={sn}) ===\n")

    # 方式1：CubeProgrammer 的 coreReg（读 PC/SP/LR）
    print("--- coreReg 快照 ---")
    out = run([CLI, "-c", f"port=SWD sn={sn} mode=hotplug", "-coreReg"]).stdout
    for ln in out.splitlines():
        if any(k in ln for k in ("PC", "SP", "LR", "XPSR", "R0", "R1")):
            print("  " + ln.strip())

    # 方式2：读 SysTick（0xE000E018 VAL），两次对比
    print("\n--- SysTick 是否在走（读两次）---")
    v1, _ = rd32(sn, 0xE000E018)
    time.sleep(0.5)
    v2, _ = rd32(sn, 0xE000E018)
    if v1 and v2:
        print(f"  SysTick->VAL 第1次 = 0x{v1[0]:08X}")
        print(f"  SysTick->VAL 第2次 = 0x{v2[0]:08X}")
        print(f"  {'不同（在跑）' if v1[0] != v2[0] else '相同（已停/被 halt）'}")
    else:
        print("  读取失败")

    # 方式3：读 RCC 时钟使能，确认外设时钟
    print("\n--- RCC APB1ENR1 (FDCAN/USART1) ---")
    v, _ = rd32(sn, 0x40021058)  # RCC_APB1ENR1
    if v:
        print(f"  RCC_APB1ENR1 = 0x{v[0]:08X}")
        print(f"    USART1EN(bit14) = {(v[0] >> 14) & 1}")
        print(f"    FDCANEN(bit25)  = {(v[0] >> 25) & 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
