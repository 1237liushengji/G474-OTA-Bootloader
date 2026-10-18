#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write_reset_bisect.py —— 二分定位：到底是第几次 write 触发的复位。

做法：静默打开 -> 每次只写 8 字节 -> 每写一次立刻短读一次，判断是否出现启动横幅。
      一旦首次出现横幅，就知道是"第几次 write"造成的。
      同时给出该次 write 之前的静默时长（防止把周期性因素误判为 write 因果）。
用法：python tools/write_reset_bisect.py COM3
"""
import sys, time, serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"

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
print("OPEN %s dtr=False rts=False CTS=%s" % (port, ser.cts), flush=True)

# 静默 4s 确认存活，同时清空缓冲
t0 = time.time()
q = bytearray()
while (time.time() - t0) < 4.0:
    c = ser.read(512)
    if c:
        q.extend(c)
print("quiet4s: bytes=%d uartLines=%d" % (len(q), bytes(q).count(b"[uart]")), flush=True)

payload = b"ABCDEFGH"   # 8 字节
sent = 0
t_start = time.time()
found = None
for i in range(1, 201):
    ser.write(payload)
    sent += len(payload)
    t_w = time.time()
    chunk = bytearray()
    while (time.time() - t_w) < 0.40:
        c = ser.read(512)
        if c:
            chunk.extend(c)
    txt = bytes(chunk).decode("utf-8", "replace")
    if chunk and (("BL:" in txt) or ("STM32G474" in txt)):
        found = i
        print("!! RESET detected on write #%d (sent=%d B, t=%.2fs)" % (i, sent, time.time() - t_start), flush=True)
        print("   content: %r" % txt[:200], flush=True)
        # 继续观察 1.5s 收完整横幅
        t_x = time.time()
        more = bytearray()
        while (time.time() - t_x) < 1.5:
            c = ser.read(512)
            if c:
                more.extend(c)
        print("   follow-up: %r" % bytes(more).decode("utf-8", "replace")[:400], flush=True)
        break
    time.sleep(0.02)

if found is None:
    print("NO reset in 200 writes (sent=%d B over %.1fs)" % (sent, time.time() - t_start), flush=True)
ser.close()
print("CLOSED", flush=True)
