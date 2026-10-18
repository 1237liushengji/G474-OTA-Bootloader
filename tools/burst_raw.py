#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
burst_raw.py —— 满负荷发送 + 打印原始收到的每一行（不做过滤）。
用法：python tools/burst_raw.py COM3 14
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
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 14.0

ser = open_safe(port)
print("OPEN %s" % port)

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
buf = bytearray()
while (time.time() - t0) < secs:
    c = ser.read(1024)
    if c:
        buf.extend(c)

stop.set()
th.join(timeout=0.3)
ser.close()

print("发送 %d 字节；收到 %d 字节" % (tx[0], len(buf)))
print("---- 原始内容 ----")
txt = bytes(buf).decode("utf-8", "replace")
print(txt)
print("---- END ----")
