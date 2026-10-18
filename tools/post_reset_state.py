#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""post_reset_state.py —— 打开端口触发复位后，立刻用外部工具检查启动是否成功。

本脚本：打开 COM（dtr=False rts=False）-> 发 1 字节触发 ~3.2s 复位
        -> 抓取复位瞬间起的完整输出，判定是否成功进入 APP。
外部配合：SWD 在复位后立即读 PC/核心状态。
用法：python tools/post_reset_state.py COM3
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
print("OPEN", flush=True)

# 触发
s.write(b"Q")
t0 = time.time()
buf = bytearray()
while (time.time() - t0) < 9.0:
    c = s.read(2048)
    if c:
        buf.extend(c)
txt = bytes(buf).decode("utf-8", "replace")
print("bytes=%d" % len(buf), flush=True)
print("---- RAW ----")
print(txt)
print("---- END ----")
print("counts: BL_clock=%d APPBanner=%d fp=%d main_led=%d help=%d uartDiag=%d"
      % (txt.count("BL: clock"), txt.count("STM32G474 CAN Node"),
         txt.count("[FP]"), txt.count("led-bsrr-state"),
         txt.count("type 'help'"), txt.count("[uart]")), flush=True)
s.close()
