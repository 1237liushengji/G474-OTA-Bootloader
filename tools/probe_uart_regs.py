#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
不依赖串口通信，直接用 SWD 读 USART1 寄存器 + GPIOA 状态，
判定"串口 RX 到底卡在哪"：
  - CR1  : 0x002D(UE|RE|TE|RXNEIE) 正确；缺 RE/RXNEIE = 未挂接收
  - ISR  : 若 RXNE 已置位但 rxIrq=0 -> 中断没进
  - MODER: PA9/PA10 应为 0b10(复用)
  - IDR  : PA10 空闲应为 1

用法:
    python probe_uart_regs.py --sn 8
"""
import argparse
import re
import subprocess

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"

USART1_BASE = 0x40013800
GPIOA_BASE = 0x48000000
NVIC_ISER1 = 0xE000E104


def rd32(sn: str, addr: int, nbytes: int = 4) -> str:
    r = subprocess.run(
        [CLI, "-c", f"port=SWD sn={sn} mode=hotplug", "-r32", hex(addr), str(nbytes)],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout


def read_word(sn: str, addr: int):
    out = rd32(sn, addr, 4)
    m = re.search(r"0x[0-9A-Fa-f]{8}\s*:\s*([0-9A-Fa-f ]+)", out)
    if not m:
        return None
    return int(m.group(1).strip().split()[0], 16)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    args = ap.parse_args()
    sn = args.sn

    print(f"=== USART1 / GPIOA 寄存器快照 (SN={sn}) ===\n")

    cr1 = read_word(sn, USART1_BASE + 0x00)
    cr2 = read_word(sn, USART1_BASE + 0x04)
    cr3 = read_word(sn, USART1_BASE + 0x08)
    brr = read_word(sn, USART1_BASE + 0x0C)
    isr = read_word(sn, USART1_BASE + 0x1C)
    rdr = read_word(sn, USART1_BASE + 0x24)
    moder = read_word(sn, GPIOA_BASE + 0x00)
    idr = read_word(sn, GPIOA_BASE + 0x10)
    iser1 = read_word(sn, NVIC_ISER1)

    def h(v):
        return "读失败" if v is None else f"0x{v:08X}"

    print(f"USART1_CR1   = {h(cr1)}")
    if cr1 is not None:
        print(f"    UE={cr1 & 1} RE={(cr1 >> 2) & 1} TE={(cr1 >> 3) & 1} "
              f"RXNEIE={(cr1 >> 5) & 1}  (期望 UE=1 RE=1 TE=1 RXNEIE=1)")
    print(f"USART1_CR2   = {h(cr2)}")
    print(f"USART1_CR3   = {h(cr3)}")
    print(f"USART1_BRR   = {h(brr)}")
    print(f"USART1_ISR   = {h(isr)}")
    if isr is not None:
        print(f"    RXNE={(isr >> 5) & 1} ORE={(isr >> 3) & 1} FE={(isr >> 1) & 1} "
              f"NE={(isr >> 2) & 1} PE={isr & 1} TXE={(isr >> 7) & 1} TC={(isr >> 6) & 1}")
    print(f"USART1_RDR   = {h(rdr)}")
    print(f"GPIOA_MODER  = {h(moder)}")
    if moder is not None:
        pa9 = (moder >> 18) & 3
        pa10 = (moder >> 20) & 3
        print(f"    PA9={pa9:02b} PA10={pa10:02b}  (期望 10=复用)")
    print(f"GPIOA_IDR    = {h(idr)}")
    if idr is not None:
        print(f"    PA9(TX)={ (idr >> 9) & 1 } PA10(RX)={ (idr >> 10) & 1 }  (RX 空闲应=1)")
    print(f"NVIC_ISER1   = {h(iser1)}")
    if iser1 is not None:
        print(f"    USART1_IRQn(37) 使能位 = {(iser1 >> 5) & 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
