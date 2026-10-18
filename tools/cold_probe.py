#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cold_probe.py —— 长时间嗅探 + 期间提示用户拔插 USB 触发真实上电复位。

用途：区分「MCU 没输出」与「打开串口瞬间的脉冲把日志吃掉了」。
    - 先静默嗅探 3 秒（基线）
    - 提示用户拔插 USB 线（真实冷启动）
    - 继续嗅探 8 秒，捕获上电启动横幅

用法：
    python tools/cold_probe.py COM3
        （脚本会在中途提示，请按提示拔插 USB 线）
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
    ser.timeout = 0.05
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    print("=" * 64)
    print("cold_probe：%s —— 长时间嗅探 + 拔插 USB 冷启动捕获" % port)
    print("=" * 64)

    try:
        ser = open_safe(port)
    except Exception as e:
        print("!! 打开失败：%s" % e)
        return

    print("已打开。")
    print("\n【阶段 1】静默嗅探 3 秒（基线，不要动任何东西）...")
    t0 = time.time()
    n1 = 0
    while (time.time() - t0) < 3.0:
        n1 += len(ser.read(256))
    print("   基线收到 %d 字节" % n1)

    print("\n【阶段 2】>>> 请现在拔掉 USB 线，等 2 秒，再插回去 <<<")
    print("   按回车后开始计时...", end="")
    try:
        input()
    except EOFError:
        pass

    print("\n【阶段 3】捕获 8 秒（含上电启动横幅）...")
    buf = bytearray()
    t0 = time.time()
    events = []
    while (time.time() - t0) < 8.0:
        chunk = ser.read(256)
        if chunk:
            now = time.time() - t0
            buf.extend(chunk)
            for ln in bytes(chunk).decode("utf-8", "replace").replace(
                    "\r\n", "\n").replace("\r", "\n").split("\n"):
                if ln.strip():
                    events.append((now, ln.strip()))

    ser.close()

    print("-" * 64)
    print("阶段 3 收到 %d 字节，%d 行" % (len(buf), len(events)))
    for dt, ln in events[:25]:
        print("   t=%.2fs | %s" % (dt, ln[:72]))

    print("\n" + "=" * 64)
    if len(buf) == 0 and n1 == 0:
        print("结论：全程零字节。这块板经 CH340K 的 TX 通路**没有任何数据**。")
        print("      LED 在闪说明 MCU 在跑 => 问题在 MCU→CH340K 的 TX 段，")
        print("      或该固件本身不打印，或你插的这块不是跑诊断固件的那块。")
    elif n1 > 0:
        print("结论：静默期间就有输出 —— MCU 在周期性打印，TX 通路正常。")
        print("      => 之前 RX 不通的问题需重新审视（MCU 侧可能真有接收到，")
        print("         只是解析/回显没做）。")
    else:
        print("结论：静默期无输出、拔插后有输出 —— 属于正常的「只在上电时打印」。")
        print("      => 若拔插后仍无输出，TX 通路有问题。")
    print("=" * 64)


if __name__ == "__main__":
    main()
