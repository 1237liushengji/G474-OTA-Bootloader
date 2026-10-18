#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rts_hold_txrx.py —— 判定实验：RTS 保持 True 时，TX 是否正常、RX 是否有可能。

背景（已确证）：
  dtr/rts 矩阵结果反直觉 —— rts=True 时 write() 不复位板子，但收不到任何字节。
  必须排除两种可能：
    (A) rts=True 导致板子根本没跑（TX 也死）   -> 若复位后仍收不到启动横幅，则 (A) 成立
    (B) 板子在跑，但 RX 方向始终收不到         -> 收到横幅但收不到回显，则 (B) 成立

实验设计（分两阶段，中间不打任何一个字）：
  Phase 1  OPEN(rts=True) 后静默 6s，抓取 --- 此时应有 BL/APP 启动横幅（若我们期间复位过）
  Phase 2  继续静默 6s，抓 [uart] 诊断行     --- 这些行是 3000ms 周期打印，能证明 TX 活着
  Phase 3  发送 8s，统计板子是否有任何反应（复位次数 / rxIrq / edges）
  Phase 4  再静默 4s，看 Phase3 期间发送的字是否触发了任何回显
用法：python tools/rts_hold_txrx.py COM3
"""
import sys, time, serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
BAUD = 115200


def open_hold_rts(port):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
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
    # 打开后再显式拉高 RTS，并在整个会话期间保持
    ser.rts = True
    time.sleep(0.2)
    return ser


def drain(ser, secs, label):
    buf = bytearray()
    t0 = time.time()
    first = None
    while (time.time() - t0) < secs:
        c = ser.read(512)
        if c:
            if first is None:
                first = time.time() - t0
            buf.extend(c)
    txt = bytes(buf).decode("utf-8", "replace")
    n_boot = txt.count("BL:") + txt.count("STM32G474") + txt.count("main:")
    n_diag = txt.count("[uart]")
    print("== %-28s bytes=%-6d first=%-7s BLbanner=%d uartLines=%d"
          % (label, len(buf), ("%.3fs" % first) if first is not None else "-", n_boot, n_diag))
    if buf:
        print("---- %s RAW ----" % label)
        print(txt)
        print("---- END ----")
    return txt


ser = open_hold_rts(port)
print("OPEN %s dtr=False rts=True  CTS=%s DSR=%s DCD=%s" % (port, ser.cts, ser.dsr, ser.cd))

drain(ser, 6.0, "P1 silent-after-open")
drain(ser, 6.0, "P2 silent-diag-window")

# Phase 3: 发送
payload = b"PING-PING-PING-0123456789\r\n"
tx = 0
t0 = time.time()
rxbuf = bytearray()
while (time.time() - t0) < 8.0:
    ser.write(payload)
    tx += len(payload)
    c = ser.read(512)
    if c:
        rxbuf.extend(c)
print("== P3 send 8s: tx=%d bytes, rx=%d bytes" % (tx, len(rxbuf)))
if rxbuf:
    print("---- P3 RAW ----")
    print(bytes(rxbuf).decode("utf-8", "replace"))
    print("---- END ----")

drain(ser, 4.0, "P4 settle-after-send")

ser.rts = False
ser.close()
print("CLOSED (rts released)")
