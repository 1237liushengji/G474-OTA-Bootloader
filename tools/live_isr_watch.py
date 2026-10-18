#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_isr_watch.py —— 持续发送的同时，观察 MCU 侧 ISR / edges / rxIrq 是否随发送变化。

设计要点（严谨性）：
  1. 不拔插任何线 —— 排除"插拔导致复位"的干扰
  2. 分两个阶段：
       Phase-1 静默 9 秒：采集 3 条基线诊断行（不发送）
       Phase-2 发送 9 秒：持续写 0x55，同时采集 3 条诊断行
     对比两阶段的 ISR / edges / rxIrq 差异 —— 这是唯一的因果证据
  3. 若 rxIrq 仍为 0 但 edges>0 -> 信号到引脚、USART 收不到
     若 edges 也恒 0         -> 信号根本没到 PA10
     若 ISR 出现 FE/NE/ORE   -> 波特率/电气问题（并给出方向）

用法：python tools/live_isr_watch.py COM3
"""
import re
import sys
import time

try:
    import serial
except ImportError:
    print("需要 pyserial")
    sys.exit(1)


def open_safe(port, baud=115200):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = 0.05
    ser.write_timeout = 0.5
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def collect(ser, seconds, do_send=False, tag=""):
    """采集成诊断行 + 元数据"""
    t0 = time.time()
    buf = bytearray()
    n_tx = 0
    last_tx = 0.0
    while (time.time() - t0) < seconds:
        if do_send and (time.time() - t0 - last_tx) >= 0.02:
            try:
                ser.write(b"UUU")     # 0x55 x3
                n_tx += 1
            except Exception:
                pass
            last_tx = time.time() - t0
        c = ser.read(512)
        if c:
            buf.extend(c)
    return bytes(buf), n_tx


def extract(blob):
    s = blob.decode("utf-8", "replace")
    d = {}
    for key, rx in [("CR1", r"CR1=([0-9A-Fa-f]+)"),
                    ("CR3", r"CR3=([0-9A-Fa-f]+)"),
                    ("BRR", r"BRR=([0-9A-Fa-f]+)"),
                    ("ISR", r"ISR=([0-9A-Fa-f]+)"),
                    ("MODER", r"MODER=([0-9A-Fa-f]+)"),
                    ("IDR", r"IDR=([0-9A-Fa-f]+)"),
                    ("edges", r"edges/20ms=(\d+)"),
                    ("rxIrq", r"rxIrq=(\d+)"),
                    ("drop", r"drop=(\d+)"),
                    ("gState", r"gState=(\d+)"),
                    ("RxState", r"RxState=(\d+)")]:
        vals = re.findall(rx, s)
        d[key] = list(dict.fromkeys(vals))   # 去重保序
    return d


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    print("=" * 70)
    print("live_isr_watch：%s —— 静默 9s  vs  发送 9s  对比" % port)
    print("=" * 70)

    try:
        ser = open_safe(port)
    except Exception as e:
        print("!! 打开失败：%s" % e)
        return

    print("\n【Phase-1】静默 9 秒（不发送，采集基线）...")
    b1, _ = collect(ser, 9.0, do_send=False)
    e1 = extract(b1)
    print("   baseline bytes=%d" % len(b1))

    print("\n【Phase-2】持续发送 9 秒（每 20ms 写 0x55x3）...")
    b2, ntx = collect(ser, 9.0, do_send=True)
    e2 = extract(b2)
    print("   sent=%d 组 (每组 3 字节 = %d 字节)" % (ntx, ntx * 3))

    ser.close()

    print("\n" + "-" * 70)
    print("%-9s | %-26s | %-26s" % ("寄存器", "Phase-1 静默", "Phase-2 发送"))
    print("-" * 70)
    for k in ["CR1", "CR3", "BRR", "ISR", "MODER", "IDR", "edges", "rxIrq",
              "drop", "gState", "RxState"]:
        v1 = ",".join(e1.get(k, []) or ["(无)"])
        v2 = ",".join(e2.get(k, []) or ["(无)"])
        print("%-9s | %-26s | %-26s" % (k, v1[:26], v2[:26]))
    print("-" * 70)

    print("\n" + "=" * 70)
    print("判据（对比两阶段）：")

    def toset(v):
        return set(v or [])

    e1_edges = toset(e1.get("edges"))
    e2_edges = toset(e2.get("edges"))
    e1_irq = toset(e1.get("rxIrq"))
    e2_irq = toset(e2.get("rxIrq"))
    e2_isr = toset(e2.get("ISR"))
    e1_isr = toset(e1.get("ISR"))

    if e1_edges == {"0"} and e2_edges == {"0"}:
        print("  [A] edges 两阶段都恒 0")
        print("      => 我发的 0x55 **没有任何电平活动到达 PA10**。")
        print("      => 结论：PC→板 的物理 RX 通路（CH340K TXD → PA10）无信号。")
    elif e2_edges != e1_edges:
        print("  [B] edges 在发送阶段发生变化：静默=%s 发送=%s" % (sorted(e1_edges), sorted(e2_edges)))
        print("      => **信号确实到达了 PA10！** MCU 侧引脚有活动。")
        if e2_irq != {"0"}:
            print("      => 且 rxIrq 非 0（%s）—— RX 完全正常！" % sorted(e2_irq))
        else:
            print("      => 但 rxIrq 仍 0 —— 信号到了引脚却没被 USART 采到，")
            print("         需查：波特率实际值 / 信号完整性 / 引脚是否真接 USART1。")
    else:
        print("  [?] edges 两阶段都是 %s（未变化）" % sorted(e2_edges))

    if e1_isr != e2_isr:
        print("  [C] ISR 在发送阶段变化：%s -> %s" % (sorted(e1_isr), sorted(e2_isr)))
    else:
        print("  [C] ISR 两阶段相同：%s" % sorted(e2_isr))

    if e2_irq != {"0"}:
        print("  [D] **rxIrq 非 0 —— RX 通了！**")
    else:
        print("  [D] rxIrq 恒 0 —— MCU 从未收到字节。")
    print("=" * 70)


if __name__ == "__main__":
    main()
