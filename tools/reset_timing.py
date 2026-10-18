#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reset_timing.py —— 区分"复位由累计时长触发"还是"由数据量触发"。

三组对照，每组独立重开端口：
  A) 打开后静默 T 秒        -> 期望 0 复位（已有 30s 数据）
  B) 打开后只发 1 次 1 字节 -> 看是否仍会在 ~3.4s 出现横幅
  C) 打开后持续满负荷发送    -> 看复位间隔
若 B 组仍在 ~3.4s 复位，则"与数据量无关，只与打开/首次发送后的时长有关"成立。
用法：python tools/reset_timing.py COM3
"""
import sys, time, serial, re

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"


def op(port, dtr=False, rts=False):
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
    s.dtr = dtr
    s.rts = rts
    s.open()
    return s


def watch(ser, secs, do_write):
    """返回 (bytes, boot_times[])"""
    buf = bytearray()
    boots = []
    t0 = time.time()
    while (time.time() - t0) < secs:
        if do_write == "burst":
            ser.write(b"U" * 256)
        elif do_write == "once":
            if not hasattr(watch, "_done"):
                ser.write(b"Q")
                watch._done = True
        c = ser.read(1024)
        if c:
            buf.extend(c)
            if c.count(b"BL: clock") or c.count(b"STM32G474"):
                boots.append(round(time.time() - t0, 2))
    if hasattr(watch, "_done"):
        del watch._done
    return bytes(buf), boots


def close(ser):
    try:
        ser.dtr = False
        ser.rts = False
        ser.close()
    except Exception:
        pass


# ---- A: 静默 ----
s = op(port)
b, boots = watch(s, 12.0, "none")
print("A silent 12s      : bytes=%-6d boots=%s" % (len(b), boots), flush=True)
close(s)
time.sleep(0.5)

# ---- B: 只发一次 ----
s = op(port)
b, boots = watch(s, 12.0, "once")
print("B single-write 12s: bytes=%-6d boots=%s" % (len(b), boots), flush=True)
close(s)
time.sleep(0.5)

# ---- C: 满负荷 ----
s = op(port)
b, boots = watch(s, 12.0, "burst")
print("C burst 12s       : bytes=%-6d boots=%s" % (len(b), boots), flush=True)
print("   (uppercase 'U' is our TX echo if it ever loops back)")
print("   U-echo count in RX: %d ; banners: %d"
      % (b.count(b"UUUU"), len(boots)), flush=True)
close(s)
