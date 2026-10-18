#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quick_sniff.py —— 快速嗅探一个 COM 口，看它到底有没有在输出。

用法：
    python tools/quick_sniff.py COM5 6
      argv[1] = 端口名（默认 COM5）
      argv[2] = 抓取秒数（默认 6）

关键点：
    - open 前显式 dtr=False / rts=False（本板 CH340K 的 DTR→BOOT0 / RTS→NRST
      会在打开瞬间产生复位脉冲，必须避免误判为"板子有输出"）
    - 打开后立即开始读，统计字节数与首行样本
"""
import sys
import time

try:
    import serial
except ImportError:
    print("需要 pyserial：pip install pyserial")
    sys.exit(1)


def open_safe(port, baud=115200):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = 0.1
    ser.write_timeout = 0.5
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0

    print("=" * 60)
    print("嗅探 %s，%.1f 秒（dtr=False rts=False）" % (port, secs))
    print("=" * 60)

    try:
        ser = open_safe(port)
    except Exception as e:
        print("!! 打开失败：%s" % e)
        return

    print("已打开。状态线： CTS=%s DSR=%s DCD=%s" % (ser.cts, ser.dsr, ser.cd))

    buf = bytearray()
    t0 = time.time()
    first_t = None
    while (time.time() - t0) < secs:
        chunk = ser.read(256)
        if chunk:
            if first_t is None:
                first_t = time.time() - t0
            buf.extend(chunk)

    ser.close()

    print("-" * 60)
    print("收到 %d 字节；首个字节出现在 t=%.3fs" % (len(buf), first_t or -1))
    if buf:
        txt = buf.decode("utf-8", "replace")
        lines = txt.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        print("前 20 行：")
        for ln in lines[:20]:
            if ln:
                print("   | " + ln)
    else:
        print(">>> 零字节。这个口此刻没有数据出来 <<<")
    print("=" * 60)


if __name__ == "__main__":
    main()
