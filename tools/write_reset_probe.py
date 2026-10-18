#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_reset_probe.py —— 精确判定"write() 是否导致板子复位"。

设计：
  - 打开端口（dtr=False rts=False）
  - 阶段1：静默 8 秒，只读不发 -> 统计 BL 横幅出现次数（应为 0）
  - 阶段2：持续 write 8 秒     -> 统计 BL 横幅出现次数
  以"BL: clock HSE+PLL"这一行的出现次数作为复位计数（每次复位必打一次）

用法：python tools/write_reset_probe.py COM3
"""
import sys
import time
import threading

try:
    import serial
except ImportError:
    print("需要 pyserial"); sys.exit(1)


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


port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
ser = open_safe(port)
print("OPEN %s (dtr=False rts=False)" % port)

# ---------- Phase 1: 静默 8s ----------
t0 = time.time()
buf1 = bytearray()
while time.time() - t0 < 8.0:
    c = ser.read(1024)
    if c:
        buf1.extend(c)
n1 = buf1.decode("utf-8", "replace").count("BL: clock HSE+PLL")
print("Phase-1 静默 8s : 收到 %d 字节, BL 横幅 %d 次" % (len(buf1), n1))

# ---------- Phase 2: 发送 8s ----------
stop = threading.Event()
tx = [0]


def sender():
    while not stop.is_set():
        try:
            ser.write(b"U" * 20)
            tx[0] += 20
        except Exception:
            pass
        time.sleep(0.002)


th = threading.Thread(target=sender, daemon=True)
th.start()
t0 = time.time()
buf2 = bytearray()
while time.time() - t0 < 8.0:
    c = ser.read(1024)
    if c:
        buf2.extend(c)
stop.set()
th.join(timeout=0.3)
ser.close()

n2 = buf2.decode("utf-8", "replace").count("BL: clock HSE+PLL")
print("Phase-2 发送 8s : 发送 %d 字节, 收到 %d 字节, BL 横幅 %d 次" % (tx[0], len(buf2), n2))

print()
print("=" * 62)
print("判据：")
print("  Phase-1(不发) 复位 %d 次 ; Phase-2(发) 复位 %d 次" % (n1, n2))
if n1 == 0 and n2 == 0:
    print("  => 发送**不**导致复位。板子稳定。")
elif n1 == 0 and n2 > 0:
    print("  => **发送导致复位**（%d 次）—— write() 触发了 NRST！" % n2)
    print("     与参考工程记录的 RTS->NRST 一键下载电路行为一致。")
else:
    print("  => 两阶段都有复位，排除干扰后重测。")
print("=" * 62)
