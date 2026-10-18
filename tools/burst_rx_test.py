#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
burst_rx_test.py —— 消除"诊断采样窗口偏差"的 RX 判定实验。

背景（为什么要这样设计）：
  固件 BSP_UART_DiagTask() 每 3 秒才做一次 20ms 的 PA10 跳变计数。
  若 PC 间断发送，20ms 窗口很可能落在"没发的时段" -> edges 采到 0，
  但这**不能**证明信号没到。必须让 PC **全程满负荷发送**，
  使得任何 20ms 窗口内都必然有信号活动。

本脚本：
  1. 打开端口
  2. 采集 baseline（静默）阶段：尽量快速连发 9 秒（每 20ms 20 字节 ≈ 10000bps）
  3. 同时记录所有 [uart] 行
  4. 输出 edges/rxIrq/ISR 的**全部**观测值（不去重）

用法：python tools/burst_rx_test.py COM3 12
"""
import re
import sys
import time
import threading

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
    ser.timeout = 0.02
    ser.write_timeout = 0.5
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

    print("=" * 70)
    print("burst_rx_test：%s，%.0f 秒 —— 满负荷发送以消除采样偏差" % (port, secs))
    print("=" * 70)

    try:
        ser = open_safe(port)
    except Exception as e:
        print("!! 打开失败：%s" % e)
        return

    # 后台线程：以 ~115200bps 满速发送 'A'（每 2ms 写 20 字节）
    stop = threading.Event()
    tx_bytes = [0]

    def sender():
        while not stop.is_set():
            try:
                ser.write(b"A" * 20)
                tx_bytes[0] += 20
            except Exception:
                pass
            time.sleep(0.002)

    th = threading.Thread(target=sender, daemon=True)
    th.start()

    print("已开始满负荷发送（后台线程）。采集 %.0f 秒...\n" % secs)

    t0 = time.time()
    buf = bytearray()
    lines = []
    while (time.time() - t0) < secs:
        c = ser.read(1024)
        if c:
            buf.extend(c)
            for ln in bytes(c).decode("utf-8", "replace").replace(
                    "\r\n", "\n").replace("\r", "\n").split("\n"):
                ln = ln.strip()
                if ln:
                    lines.append((time.time() - t0, ln))

    stop.set()
    th.join(timeout=0.5)
    ser.close()

    print("发送总量 = %d 字节" % tx_bytes[0])
    print("收到日志 %d 行\n" % len(lines))

    # 打印所有 [uart] 相关行
    uart_lines = [x for x in lines if "[uart]" in x[1] or "edges" in x[1] or "rxIrq" in x[1]]
    print("---- 全部 [uart] 诊断行 ----")
    for dt, ln in uart_lines:
        print("  t=%5.2fs | %s" % (dt, ln[:82]))
    if not uart_lines:
        print("  (无 —— 诊断窗口可能已过期；请复位板子后立刻重跑)")

    edges = re.findall(r"edges/20ms=(\d+)", "\n".join(l for _, l in lines))
    irq = re.findall(r"rxIrq=(\d+)", "\n".join(l for _, l in lines))
    isr = re.findall(r"ISR=([0-9A-Fa-f]+)", "\n".join(l for _, l in lines))

    print("\n" + "=" * 70)
    print("edges 全部取值 : %s" % (edges if edges else "无"))
    print("rxIrq 全部取值 : %s" % (irq if irq else "无"))
    print("ISR  全部取值 : %s" % (list(dict.fromkeys(isr)) if isr else "无"))
    print("-" * 70)
    if not edges:
        print("判据：未采到诊断行 —— 需复位板子后立即重跑（90s 窗口限制）。")
    else:
        mx = max(int(x) for x in edges)
        if mx > 0:
            print("判据：**edges 最大值 = %d > 0 —— 信号到达 PA10！**" % mx)
            if irq and max(int(x) for x in irq) > 0:
                print("      且 rxIrq > 0 —— RX 通了！")
            else:
                print("      但 rxIrq 恒 0 —— 信号到引脚却未被 USART 采样，")
                print("      重点查：真实波特率 / 信号完整性 / AF 复用是否真生效。")
        else:
            print("判据：**edges 恒 0（满负荷发送下仍是 0）—— 信号未到达 PA10。**")
            print("      这是强证据：PC→板 的 RX 物理通路无信号。")
    print("=" * 70)


if __name__ == "__main__":
    main()
