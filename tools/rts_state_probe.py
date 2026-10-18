#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rts_state_probe.py —— 打开串口并保持指定 dtr/rts，期间由外部 SWD 读取核心状态。

本脚本只负责"保持串口打开 + 保持某个 RTS 电平"，不读取 SWD。
用法：python tools/rts_state_probe.py COM3 <dtr:0|1> <rts:0|1> <秒数>
"""
import sys, time, serial

port = sys.argv[1]
dtr = bool(int(sys.argv[2]))
rts = bool(int(sys.argv[3]))
secs = float(sys.argv[4]) if len(sys.argv) > 4 else 20.0

ser = serial.Serial()
ser.port = port
ser.baudrate = 115200
ser.bytesize = serial.EIGHTBITS
ser.parity = serial.PARITY_NONE
ser.stopbits = serial.STOPBITS_ONE
ser.timeout = 0.05
ser.xonxoff = False
ser.rtscts = False
ser.dsrdtr = False
ser.dtr = False
ser.rts = False
ser.open()
ser.dtr = dtr
ser.rts = rts
print("HOLD %s dtr=%s rts=%s for %.0fs  CTS=%s DSR=%s DCD=%s"
      % (port, dtr, rts, secs, ser.cts, ser.dsr, ser.cd), flush=True)

t0 = time.time()
n = 0
while (time.time() - t0) < secs:
    c = ser.read(256)
    if c:
        n += len(c)
        try:
            sys.stdout.write(c.decode("utf-8", "replace"))
            sys.stdout.flush()
        except Exception:
            pass
print("\nHOLD-DONE rx=%d bytes" % n, flush=True)
ser.dtr = False
ser.rts = False
ser.close()
print("CLOSED", flush=True)
