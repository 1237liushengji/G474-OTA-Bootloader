#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rts_pulse_after_write.py —— 验证"write 路径会翻 RTS"。

假设：rts=False 打开时，CH340K 驱动在 write() 时把 RTS 拉低（=NRST 有效）。
做法：
  1. rts=False 打开，写一次（应触发 ~3.2s 后复位）
  2. 复位刚出现后，立刻把 rts 设为 True 并保持 —— 若板子再次"死亡"
     （不再有任何输出），说明 RTS 引脚确实被 write 路径驱动过/可被驱动
  3. 再设回 False，看是否恢复
用法：python tools/rts_pulse_after_write.py COM3
"""
import sys, time, serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"

s = serial.Serial()
s.port = port
s.baudrate = 115200
s.bytesize = 8
s.parity = "N"
s.stopbits = 1
s.timeout = 0.02
s.xonxoff = False
s.rtscts = False
s.dsrdtr = False
s.dtr = False
s.rts = False
s.open()
print("OPEN rts=False", flush=True)

t0 = time.time()
buf = bytearray()
while (time.time() - t0) < 6.0:
    s.write(b"Q")
    time.sleep(0.3)
    c = s.read(1024)
    if c:
        buf.extend(c)
print("phase1 (rts=False, writing): bytes=%d banners=%d"
      % (len(buf), bytes(buf).count(b"BL: clock")), flush=True)

# 立刻断言 RTS
s.rts = True
print(">>> rts asserted True at t=%.2fs" % (time.time() - t0), flush=True)
buf2 = bytearray()
t1 = time.time()
while (time.time() - t1) < 8.0:
    c = s.read(1024)
    if c:
        buf2.extend(c)
print("phase2 (rts=True, silent): bytes=%d" % len(buf2), flush=True)

# 释放
s.rts = False
print(">>> rts released at t=%.2fs" % (time.time() - t0), flush=True)
buf3 = bytearray()
t2 = time.time()
while (time.time() - t2) < 6.0:
    c = s.read(1024)
    if c:
        buf3.extend(c)
txt3 = bytes(buf3).decode("utf-8", "replace")
print("phase3 (rts=False, silent): bytes=%d banners=%d uartLines=%d"
      % (len(buf3), txt3.count("BL: clock"), txt3.count("[uart]")), flush=True)
s.close()
print("CLOSED", flush=True)
