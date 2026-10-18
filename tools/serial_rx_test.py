#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口 RX 连通性测试：向板卡发一条控制台命令，看是否有回显/应答。
用于判定 PA10(RX) 是否真的开路。"""
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serial_safe import open_safe  # noqa: E402


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    cmd = sys.argv[2] if len(sys.argv) > 2 else "version"
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0

    ser = open_safe(port, 115200)
    if ser is None:
        print("[ERR] 打不开", port)
        return 1

    # 打开串口会触发 CH340 的 DTR/RTS 时序，板卡可能复位。
    # 先等它启完并把开机日志排空，再发命令，否则命令会落在启动过程里被吞掉。
    t_settle = time.time()
    while (time.time() - t_settle) < 8.0:
        if ser.in_waiting:
            ser.read(ser.in_waiting)
        else:
            time.sleep(0.05)
    ser.reset_input_buffer()

    payload = (cmd + "\r\n").encode("ascii")
    ser.write(payload)
    ser.flush()
    print("[..] 已发送 %r 到 %s，读取 %.1fs ..." % (payload, port, secs))

    t0 = time.time()
    got = b""
    while (time.time() - t0) < secs:
        n = ser.in_waiting
        if n:
            got += ser.read(n)
        else:
            time.sleep(0.05)
    ser.close()

    print("[..] 收到 %d 字节" % len(got))
    if got:
        text = got.decode("utf-8", "replace")
        print("----- 原文 -----")
        print(text[:2000])
        print("----------------")
        if cmd in text or "help" in text or "G474" in text:
            print("[OK ] 串口 RX 可用 —— 板卡收到了命令并有应答")
            return 0
        print("[??] 有输出但未识别到应答，需人工看原文")
        return 0

    print("[FAIL] 无任何输出：要么 RX 没收到（PA10 开路），要么板卡没在跑")
    return 2


if __name__ == "__main__":
    sys.exit(main())
