#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""passive_recv.py —— 被动抓取指定秒数内的全部串口数据并原样打印。
不发送任何东西。用法：python tools/passive_recv.py COM3 30
"""
import sys, time, serial

def open_safe(port, baud=115200):
    ser = serial.Serial()
    ser.port = port; ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS; ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE; ser.timeout = 0.05
    ser.xonxoff = False; ser.rtscts = False; ser.dsrdtr = False
    ser.dtr = False; ser.rts = False
    ser.open(); return ser

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
ser = open_safe(port)
print("OPEN %s dtr=False rts=False  CTS=%s DSR=%s DCD=%s" % (port, ser.cts, ser.dsr, ser.cd))
buf = bytearray()
t0 = time.time()
first = None
while (time.time() - t0) < secs:
    c = ser.read(256)
    if c:
        if first is None: first = time.time() - t0
        buf.extend(c)
ser.close()
print("DONE bytes=%d first=%.3fs" % (len(buf), first if first is not None else -1))
if buf:
    print("---- RAW ----")
    print(bytes(buf).decode("utf-8", "replace"))
    print("---- END ----")
else:
    print("(zero bytes)")
