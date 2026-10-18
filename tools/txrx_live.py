#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
txrx_live.py —— 发送的同时观察 MCU 侧 ISR/RXNE 变化（决定性 RX 验证）。

原理：
  固件 BSP_UART_DiagTask() 每 3 秒打印一行，含 ISR / edges / rxIrq。
  若我在它打印的间隙"连续发送"，下一行诊断就能反映发送期间 ISR 的变化：
    - 若出现 NE/FE/PE  -> 信号到了，但波特率/极性/噪声不对
    - 若 RXNE 曾置位但 rxIrq=0 -> 中断没进（NVIC/优先级问题）
    - 若 ISR 完全不变、edges 恒 0 -> 信号根本没到 PA10

本脚本：打开端口 -> 持续每 50ms 写 1 字节 -> 全程读，打印 MCU 返回的诊断行。

用法：python tools/txrx_live.py COM3 15
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
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

    print("=" * 68)
    print("txrx_live：%s —— 边发边看 MCU ISR（抓 %.0f 秒）" % (port, secs))
    print("=" * 68)

    try:
        ser = open_safe(port)
    except Exception as e:
        print("!! 打开失败：%s" % e)
        return

    print("已打开。开始【持续发送 0x55/'A' 交替】+ 读取诊断行...\n")

    t0 = time.time()
    n_tx = 0
    last_tx = 0.0
    last_hb = 0.0
    txt_all = []

    while (time.time() - t0) < secs:
        now = time.time() - t0

        # --- 每 50ms 写 2 字节（0x41 'A'）---
        if (now - last_tx) >= 0.05:
            try:
                ser.write(b"AA")
                n_tx += 1
            except Exception:
                pass
            last_tx = now

        # --- 读 ---
        chunk = ser.read(512)
        if chunk:
            s = bytes(chunk).decode("utf-8", "replace")
            txt_all.append(s)
            for ln in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                ln = ln.strip()
                if "[uart]" in ln or "rxIrq" in ln or "edges" in ln:
                    print("  t=%5.2fs | %s" % (now, ln[:80]))

    ser.close()
    joined = "".join(txt_all)

    print("\n" + "-" * 68)
    print("发送次数=%d（每次 2 字节，共 %d 字节）" % (n_tx, n_tx * 2))

    # 提取最后一条诊断
    isr = re.findall(r"ISR=([0-9A-Fa-f]+)", joined)
    edges = re.findall(r"edges/20ms=(\d+)", joined)
    irq = re.findall(r"rxIrq=(\d+)", joined)
    cr1 = re.findall(r"CR1=([0-9A-Fa-f]+)", joined)

    print("CR1   : %s" % (set(cr1) if cr1 else "无"))
    print("ISR   : %s" % (list(dict.fromkeys(isr)) if isr else "无"))
    print("edges : %s" % (list(dict.fromkeys(edges)) if edges else "无"))
    print("rxIrq : %s" % (list(dict.fromkeys(irq)) if irq else "无"))

    print("\n" + "=" * 68)
    print("判据：")
    if not isr:
        print("  未捕获到诊断行 —— 日志窗口可能已过期（>90s）。请复位板子后重跑。")
    else:
        e_set = set(edges)
        i_set = set(irq)
        isr_last = isr[-1]
        v = int(isr_last, 16)
        ne = (v >> 2) & 1
        fe = (v >> 1) & 1
        pe = v & 1
        idle = (v >> 4) & 1
        if e_set != {"0"}:
            print("  ✅ edges/20ms 出现非 0（%s）—— **信号到达了 PA10！**" % sorted(e_set))
        else:
            print("  ❌ edges/20ms 恒 0 —— 我发的 'AA' **没到 PA10**（本机 TX→板 RX 断）。")
        if i_set != {"0"}:
            print("  ✅ rxIrq 非 0（%s）—— **MCU 已收到字节！RX 通！**" % sorted(i_set))
        else:
            print("  ❌ rxIrq 恒 0 —— MCU 没收到任何字节。")
        if ne or fe or pe:
            print("  ⚠️ ISR 有错误标志 NE=%d FE=%d PE=%d —— 收到过毛刺/帧错。" % (ne, fe, pe))
        if idle:
            print("  ℹ️ IDLE=1 —— 线路上检测到过空闲边沿（有活动迹象）。")
    print("=" * 68)


if __name__ == "__main__":
    main()
