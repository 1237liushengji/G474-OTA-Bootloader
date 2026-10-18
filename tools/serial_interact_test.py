#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口交互判定：持续发送命令并全程记录，用来区分
   (a) 板卡能正常收到命令并应答
   (b) 每次 write 都触发 CH340 自动下载电路把板子复位（表现为反复出现启动日志）
   (c) PA10 根本没信号（完全没有任何启动日志变化）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serial_safe import open_safe  # noqa: E402


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    cmd = sys.argv[2] if len(sys.argv) > 2 else "version"
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0

    ser = open_safe(port, 115200)
    if ser is None:
        print("[ERR] 打不开", port)
        return 1

    buf = bytearray()
    sends = 0
    t0 = time.time()
    next_send = t0 + 3.0          # 先让它启完
    while (time.time() - t0) < secs:
        if ser.in_waiting:
            buf += ser.read(ser.in_waiting)
        else:
            time.sleep(0.02)
        if time.time() >= next_send:
            ser.write((cmd + "\r\n").encode("ascii"))
            ser.flush()
            sends += 1
            next_send = time.time() + 2.0
    ser.close()

    text = buf.decode("utf-8", "replace")
    print("[..] 发送 %d 次 %r，共收到 %d 字节" % (sends, cmd, len(buf)))
    print("----- 原文 -----")
    print(text[:3000])
    print("----------------")

    boots = text.count("BL: clock") + text.count("[boot] A:") + text.count("type 'help'")
    print("[统计] 启动日志出现次数 = %d" % boots)
    if boots >= 2:
        print("[判定] 每次 write 都触发复位 —— CH340 自动下载电路，交互式串口不可用")
        return 3
    if ("G474" in text) or ("version" in text.lower() and boots == 1):
        print("[判定] 串口可交互")
        return 0
    print("[判定] 无有效应答")
    return 2


if __name__ == "__main__":
    sys.exit(main())
