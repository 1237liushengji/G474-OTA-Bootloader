#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性读取"串口无输出"现场的全部关键寄存器，用于判定堵点。

读取项（全部走 SWD hotplug 单次 -r32 批量读）：
  0xE000E010  SysTick CTRL/LOAD/VAL/CALIB   (4*4=16B)
  0xE000ED04  ICSR / SHCSR / CFSR / HFSR    (0x04..0x2C 起)
  0xE000E100  NVIC ISER0..ISER2             (12B)
  0xE000E200  NVIC ISPR0..ISPR2             (12B)
  0x40013800  USART1 CR1/CR2/CR3/BRR/GTPR/RTOR/RQR/ISR/ICR/RDR/TDR  (11*4=44B)
  0x40021058  RCC_APB1ENR1
  0x48000000  GPIOA MODER/OTYPER/OSPEEDR/PUPDR/IDR/ODR  (6*4=24B)
  0xE0042004  DBGMCU CR   (halt 时各外设冻结位)
  0xE0042014  DBGMCU APB1FZR1
  0xE000E014  SysTick LOAD (重复确认)

用法:
    python field_dump.py --sn 8
    python field_dump.py --sn 8 --mode normal
"""
import argparse
import re
import subprocess

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"

BLOCKS = [
    (0xE000E010, 16, "SysTick CTRL/LOAD/VAL/CALIB"),
    (0xE000ED00, 20, "SCB CPUID..CFSR"),
    (0xE000ED24, 12, "SHCSR/CFSR/HFSR"),
    (0xE000ED34, 12, "MMFAR/BFAR/AFSR"),
    (0xE000E100, 12, "NVIC ISER0-2"),
    (0xE000E200, 12, "NVIC ISPR0-2"),
    (0xE000E280, 12, "NVIC IABR0-2"),
    (0xE0042004, 4,  "DBGMCU CR"),
    (0xE0042014, 4,  "DBGMCU APB1FZR1"),
    (0xE0042018, 4,  "DBGMCU APB1FZR2"),
    (0x40021058, 4,  "RCC APB1ENR1 (USART1EN=bit14)"),
    (0x48000000, 24, "GPIOA MODER..ODR"),
    (0x40013800, 44, "USART1 CR1..TDR"),
]

WORDS = {}


def read_block(sn, mode, addr, nbytes):
    r = subprocess.run(
        [CLI, "-c", f"port=SWD sn={sn} mode={mode}", "-r32", hex(addr), str(nbytes)],
        capture_output=True, text=True, timeout=90,
    )
    out = r.stdout + "\n" + r.stderr
    return out


def parse_words(out):
    """从 -r32 输出里抓 '0xADDR : W1 W2 W3 W4' 形式的十六进制字"""
    got = {}
    for m in re.finditer(r"0x([0-9A-Fa-f]{8})\s*:\s*((?:[0-9A-Fa-f]{8}\s*)+)", out):
        base = int(m.group(1), 16)
        for i, w in enumerate(m.group(2).split()):
            got[base + i * 4] = int(w, 16)
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    ap.add_argument("--mode", default="hotplug", choices=["hotplug", "normal", "UR"])
    args = ap.parse_args()

    for addr, n, name in BLOCKS:
        out = read_block(args.sn, args.mode, addr, n)
        ws = parse_words(out)
        if not ws:
            print(f"## {name} @0x{addr:08X}  <== 读失败/无数据")
            for ln in out.splitlines():
                if "Error" in ln or "error" in ln:
                    print("     " + ln.strip())
            continue
        for i in range(0, n, 4):
            a = addr + i
            v = ws.get(a, 0xDEADBEEF)
            print(f"  0x{a:08X} = {v:08X}   [{name}]")
    # 尝试读 PC
    out = read_block(args.sn, args.mode, 0, 0)
    print("\n--- 原始输出尾部（含 PC/LR/SP 行，若有）---")
    for ln in out.splitlines():
        s = ln.strip()
        if "PC" in s or "SP" in s or "LR" in s or "XPSR" in s or "Register" in s:
            print("  " + s)


if __name__ == "__main__":
    main()
