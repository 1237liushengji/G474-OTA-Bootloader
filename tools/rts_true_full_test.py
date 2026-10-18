#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rts_true_full_test.py —— 在 rts=True 下做完整验证：
  1) 打开端口（rts=True）
  2) 静默 3 秒（基线）
  3) 提示外部复位（或由调用方在另一步复位）
  4) 静默 6 秒（应捕获启动横幅 + 诊断行）- 验证 TX
  5) 持续发送 8 秒（验证 RX：诊断行的 edges/rxIrq 是否变化）
  6) 汇总

用法：python tools/rts_true_full_test.py COM3
"""
import re
import sys
import time
import threading

try:
    import serial
except ImportError:
    print("需要 pyserial"); sys.exit(1)


def open_rts_true(port, baud=115200):
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
    ser.dtr = True
    ser.rts = True
    ser.open()
    ser.dtr = True
    ser.rts = True
    return ser


port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
ser = open_rts_true(port)
print("OPEN %s (dtr=True rts=True)" % port)

# ---- 1) 基线 3s ----
buf = bytearray()
t0 = time.time()
while time.time() - t0 < 3.0:
    c = ser.read(512)
    if c:
        buf.extend(c)
print("基线 3s : %d 字节" % len(buf))

# ---- 2) 等外部复位 ----
print("\n>>> 请现在【用 ST-Link 复位】或【拔插 USB 线】触发一次复位，然后按回车 <<<")
try:
    input()
except EOFError:
    pass

# ---- 3) 静默 6s 捕获启动 ----
buf = bytearray()
t0 = time.time()
while time.time() - t0 < 6.0:
    c = ser.read(512)
    if c:
        buf.extend(c)
txt = buf.decode("utf-8", "replace")
print("复位后静默 6s : %d 字节" % len(buf))
for ln in txt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
    if ln.strip():
        print("   | " + ln.strip()[:78])

# ---- 4) 发送 8s ----
stop = threading.Event()
tx = [0]


def sender():
    while not stop.is_set():
        try:
            ser.dtr = True
            ser.rts = True
            ser.write(b"U" * 20)
            tx[0] += 20
        except Exception:
            pass
        time.sleep(0.002)


th = threading.Thread(target=sender, daemon=True)
th.start()
buf = bytearray()
t0 = time.time()
while time.time() - t0 < 8.0:
    c = ser.read(1024)
    if c:
        buf.extend(c)
stop.set()
th.join(timeout=0.3)
ser.close()

txt = buf.decode("utf-8", "replace")
print("\n发送 8s : 发送 %d 字节, 收到 %d 字节" % (tx[0], len(buf)))
for ln in txt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
    if ln.strip():
        print("   | " + ln.strip()[:78])

edges = re.findall(r"edges/200ms=(\d+)", txt)
irq = re.findall(r"rxIrq=(\d+)", txt)
print("\n edges=%s  rxIrq=%s" % (edges if edges else "无", irq if irq else "无"))
