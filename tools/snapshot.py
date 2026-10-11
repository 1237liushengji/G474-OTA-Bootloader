#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一口气读取 PC + USART1 + RCC + GPIOA（**单次 hotplug 连接内完成**），
避免多次连接/复位造成状态不一致。

用 STM32CubeProgrammer 的 `-c ... -coreReg` 与多次 `-r32` 在**一条命令**里
串起来（CLI 支持多个动作顺序执行）。

用法:
    python snapshot.py --sn 8
"""
import argparse
import re
import subprocess

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    args = ap.parse_args()
    sn = args.sn

    # 一次连接，顺序执行多个读动作
    cmd = [
        CLI, "-c", f"port=SWD sn={sn} mode=hotplug",
        "-coreReg",
        "-r32", "0x40013800", "8",      # USART1 CR1,CR2 ; ISR@0x1C
        "-r32", "0x4001381C", "8",      # ISR, BRR? (0x1C=ISR,0x20=BRR)
        "-r32", "0x48000000", "8",      # GPIOA MODER
        "-r32", "0x48000010", "4",      # GPIOA IDR
        "-r32", "0x40021058", "4",      # RCC_APB1ENR1
        "-r32", "0xE000E018", "4",      # SysTick VAL
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = r.stdout

    print("=== 单次连接快照 (SN=%s) ===\n" % sn)

    # PC/LR/SP
    for ln in out.splitlines():
        if re.match(r"\s*(PC|LR|SP|MSP|XPSR)\s+=", ln):
            print("  " + ln.strip())

    # 解析所有读回块
    def blocks():
        cur = None
        for ln in out.splitlines():
            m = re.match(r"\s*Address:\s*:\s*(0x[0-9A-Fa-f]+)", ln)
            if m:
                cur = int(m.group(1), 16)
                continue
            m = re.match(r"\s*(0x[0-9A-Fa-f]{8})\s*:\s*([0-9A-Fa-f ]+)", ln)
            if m and cur is not None:
                addr = int(m.group(1), 16)
                ws = [int(w, 16) for w in m.group(2).split() if len(w) == 8]
                yield addr, ws

    addr_map = {}
    for addr, ws in blocks():
        for i, w in enumerate(ws):
            addr_map[addr + 4 * i] = w

    def g(a):
        return addr_map.get(a)

    print("\n  --- USART1 ---")
    cr1 = g(0x40013800)
    isr = g(0x4001381C)
    if cr1 is not None:
        print(f"  CR1 = 0x{cr1:08X}  UE={cr1&1} RE={(cr1>>2)&1} TE={(cr1>>3)&1} RXNEIE={(cr1>>5)&1}")
    if isr is not None:
        print(f"  ISR = 0x{isr:08X}  TXE={(isr>>7)&1} TC={(isr>>6)&1} RXNE={(isr>>5)&1}")

    print("\n  --- GPIOA ---")
    moder = g(0x48000000)
    idr = g(0x48000010)
    if moder is not None:
        print(f"  MODER = 0x{moder:08X}  PA9={(moder>>18)&3:02b} PA10={(moder>>20)&3:02b}")
    if idr is not None:
        print(f"  IDR   = 0x{idr:08X}  PA9={(idr>>9)&1} PA10={(idr>>10)&1}")

    print("\n  --- RCC ---")
    apb1 = g(0x40021058)
    if apb1 is not None:
        print(f"  APB1ENR1 = 0x{apb1:08X}  USART1EN={(apb1>>14)&1} FDCANEN={(apb1>>25)&1}")

    print("\n  --- SysTick ---")
    val = g(0xE000E018)
    if val is not None:
        print(f"  VAL = 0x{val:08X}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
