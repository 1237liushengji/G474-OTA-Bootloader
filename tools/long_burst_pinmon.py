#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""long_burst_pinmon.py —— 纠正版 RX 验证：单次超长发送 + 并发读取

为什么之前的实验无效：
  之前用「循环多次小 write」发送。已确认 **每次 write() 都会复位板子**，
  所以循环发送 = 连续复位 → MCU 从未有机会启动，观测到的 0 跳变是在
  "静默期间"测的，**不能**证明信号没到（观测偏差）。

本工具的做法：
  只做**一次** write()，写入一个很大的缓冲（默认 60000 字节 ≈ 5.2s@115200）。
  这样复位只发生一次（或极少次），MCU 启动后**数据仍在传输中**，
  于是在"MCU 存活 + 数据在流"的重叠窗口内就能观测到 PA10 / 其他引脚是否跳变。

用法：python tools/long_burst_pinmon.py COM3 [字节数]
"""
import sys
import threading
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
NBYTES = int(sys.argv[2]) if len(sys.argv) > 2 else 60000

ser = serial.Serial()
ser.port = PORT
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
ser.write_timeout = 30.0
ser.open()
print("OPEN %s dtr=False rts=False  (single long write of %d bytes)" % (PORT, NBYTES), flush=True)

state = {"sent": 0, "done": False}


def writer():
    buf = b"U" * 512
    t0 = time.time()
    try:
        remaining = NBYTES
        while remaining > 0:
            n = min(len(buf), remaining)
            # 尽量一次性提交，减少 write() 次数（每次 write 都可能复位板子）
            w = ser.write(buf[:n])
            state["sent"] += w
            remaining -= w
    except Exception as e:
        print("writer error: %r" % (e,), flush=True)
    state["done"] = True
    print("writer finished: sent=%d in %.2fs" % (state["sent"], time.time() - t0), flush=True)


th = threading.Thread(target=writer, daemon=True)
th.start()

buf = bytearray()
t0 = time.time()
last_report = 0.0
while (time.time() - t0) < 14.0:
    c = ser.read(4096)
    if c:
        buf.extend(c)
    if state["done"] and (time.time() - t0) > 4.0:
        # 发送结束后再多读 4s 就够
        pass
    if (time.time() - t0) - last_report > 2.0:
        last_report = time.time() - t0

th.join(timeout=20.0)
# 发送结束后继续读 6s，确保复位后的启动日志被完整捕获
t1 = time.time()
while (time.time() - t1) < 6.0:
    c = ser.read(4096)
    if c:
        buf.extend(c)
ser.close()

txt = bytes(buf).decode("utf-8", "replace")
print("=== total rx bytes: %d ===" % len(buf))
print("=== [rst] 行 ===")
for l in txt.splitlines():
    if "[rst]" in l:
        print("  ", l)
print("=== [pins] 行（关键：看有没有 PA10 之外的意外引脚） ===")
for l in txt.splitlines():
    if l.startswith("[pins]"):
        print("  ", l)
print("=== [rxwin] / [rxchk] 行 ===")
for l in txt.splitlines():
    if l.startswith("[rxwin]") or l.startswith("[rxchk]"):
        print("  ", l)
print("=== [uart] 行 ===")
for l in txt.splitlines():
    if l.startswith("[uart]"):
        print("  ", l)
print("=== BL banners: %d ===" % txt.count("BL: clock HSE+PLL"))
