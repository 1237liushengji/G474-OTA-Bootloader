#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write_reset_csr.py —— 精确定位：write() 触发的复位到底是哪一路来源。

做法：
  1. 静默打开（rts=False），抓 3s，确证板子在跑
  2. 记录此刻 RCC_CSR（外部 SWD 由调用者做）
  3. 发送固定量数据，同时计时
  4. 一旦检测到新字节（说明复位后重启了），立刻打印相对时间和收到的头部内容
只依赖串口，不碰 SWD。配合外部 SWD 读 RCC_CSR。
用法：python tools/write_reset_csr.py COM3 10
"""
import sys, time, serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0


def op(port):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = 0.02
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


ser = op(port)
print("OPEN %s dtr=False rts=False  CTS=%s DSR=%s DCD=%s" % (port, ser.cts, ser.dsr, ser.cd), flush=True)

# Phase 1: 静默确认存活
t0 = time.time()
q = bytearray()
while (time.time() - t0) < 3.0:
    c = ser.read(512)
    if c:
        q.extend(c)
print("P1 quiet3s: bytes=%d uartLines=%d" % (len(q), bytes(q).count(b"[uart]")), flush=True)

# Phase 2: 每次只发一小包，包间长间隔，观察复位是否由"某一次 write"触发
payload = b"X" * 64
events = []
t_start = time.time()
sent = 0
last_rx = time.time()
rx_total = 0
while (time.time() - t_start) < secs:
    ser.write(payload)
    sent += len(payload)
    # 读一小会儿，看是否有新的启动横幅
    t_r = time.time()
    chunk = bytearray()
    while (time.time() - t_r) < 0.25:
        c = ser.read(512)
        if c:
            chunk.extend(c)
    if chunk:
        rx_total += len(chunk)
        txt = bytes(chunk).decode("utf-8", "replace")
        is_boot = ("BL:" in txt) or ("STM32G474" in txt) or ("main:" in txt)
        events.append((time.time() - t_start, sent, len(chunk), is_boot))
        if is_boot:
            print("  >> BOOT @ t=%.2fs after sent=%d : %r" % (time.time() - t_start, sent, txt[:120]), flush=True)
    time.sleep(0.05)

print("P2 send %.0fs: sent=%d rx=%d  rxEvents=%d  bootEvents=%d"
      % (secs, sent, rx_total, len(events), sum(1 for e in events if e[3])), flush=True)
ser.close()
print("CLOSED", flush=True)
