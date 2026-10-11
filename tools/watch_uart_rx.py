#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时监测 USART1 接收侧状态（不依赖串口通信，纯 SWD 读寄存器）。

原理（关键）：
  115200bps 下一个字符约 87us，SWD 单次读要 1~3ms——**用 SWD 轮询抓不到位跳变**。
  但 USART1 硬件自己会记录：
    - ISR.RXNE : 收到字节且未被读走（1 = 有新数据在等）
    - ISR.ORE  : 接收溢出（前一字节没读走又来新字节）——**这是"有数据到达"的铁证**
    - RDR      : 读走前的数据
  所以策略：**不做任何读操作**，只反复看 RXNE/ORE。若你敲字符时有任一置位，
  就证明"信号确实到达了 USART1 内核"——那问题只可能在中断/DMA 层；
  若始终为 0，则信号根本没到 PA10（物理层断）。

用法:
    python watch_uart_rx.py --sn 8 --secs 20
"""
import argparse
import re
import subprocess
import time

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"
USART1_ISR = 0x4001381C
GPIOA_IDR = 0x48000010


def read_block(sn: str, addr: int, nbytes: int):
    """一次性读一段内存，返回字节列表（比逐字读快）。"""
    r = subprocess.run(
        [CLI, "-c", f"port=SWD sn={sn} mode=hotplug", "-r32", hex(addr), str(nbytes)],
        capture_output=True, text=True, timeout=30,
    )
    vals = []
    for m in re.finditer(r"0x[0-9A-Fa-f]{8}\s*:\s*([0-9A-Fa-f ]+)", r.stdout):
        for w in m.group(1).split():
            if len(w) == 8:
                vals.append(int(w, 16))
    return vals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=0.15)
    args = ap.parse_args()
    sn = args.sn

    print(f"=== 监测 USART1 RX (SN={sn})，持续 {args.secs}s ===")
    print(">>> 请现在开始【连续敲键盘字符】（比如轮着敲 a s d f），不要停 <<<\n")

    # 连续地址读：ISR(0x4001381C) 到 RDR(0x40013824) 共 12 字节，
    # 但为简单起见分两次读 ISR 和 IDR
    t_end = time.time() + args.secs
    rxne_hits = 0
    ore_hits = 0
    fe_hits = 0
    pa10_changes = 0
    prev_pa10 = None
    samples = 0
    t_start = time.time()

    while time.time() < t_end:
        isr_vals = read_block(sn, USART1_ISR, 4)
        idr_vals = read_block(sn, GPIOA_IDR, 4)
        if isr_vals and idr_vals:
            isr = isr_vals[0]
            idr = idr_vals[0]
            samples += 1
            if (isr >> 5) & 1:
                rxne_hits += 1
            if (isr >> 3) & 1:
                ore_hits += 1
            if (isr >> 1) & 1:
                fe_hits += 1
            pa10 = (idr >> 10) & 1
            if prev_pa10 is not None and pa10 != prev_pa10:
                pa10_changes += 1
            prev_pa10 = pa10
        time.sleep(args.interval)

    dt = time.time() - t_start
    print(f"\n=== 结果（采样 {samples} 次 / {dt:.1f}s，约 {samples/dt:.0f} Hz）===")
    print(f"  RXNE 置位次数 : {rxne_hits}")
    print(f"  ORE  置位次数 : {ore_hits}   <-- >0 = 有数据到达过（铁证）")
    print(f"  FE   置位次数 : {fe_hits}")
    print(f"  PA10 电平变化 : {pa10_changes}")
    print()
    if rxne_hits or ore_hits or fe_hits:
        print("  ==> 信号【到达了 USART1】，问题在中断/DMA/软件层")
    elif pa10_changes:
        print("  ==> PA10 有电平变化但 USART1 无反应 -> 波特率/采样点或引脚复用异常")
    else:
        print("  ==> PA10 电平恒定且 USART1 无任何 RX 事件")
        print("      => 信号根本没到 PA10（物理层：板载USB串口TX未接PA10，或线/座子问题）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
