#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reset_probe.py —— 判定"打开串口是否复位 MCU"的决定性实验。

原理：
  板载 CH340K 的 RTS→NRST / DTR→BOOT0（一键下载电路）会在"打开串口"瞬间
  产生电平脉冲。若它复位了 MCU，则：
    - 打开后极短时间内（<500ms）会出现**启动横幅**
    - 之后彻底静默（因为 MCU 刚复位，主循环还没跑起来，或又在被复位）
  若 MCU 没被复位，则：
    - 打开后立刻静默（错过的日志不会再补发）

判据：
  A) 打开后 t<0.5s 内出现 "BL:" 或 "main:" 等启动字样  -> 打开了就复位了
  B) t=0~2s 内持续有周期性输出（如 [uart] 诊断行/心跳）  -> MCU 在正常跑，RX 问题另找
  C) 全程零字节                                      -> 打开不复位、且 MCU 此刻无输出

用法：
  python tools/reset_probe.py COM3
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
    print("reset_probe：%s —— 判定「打开串口是否复位 MCU」" % port)
    print("=" * 64)

    try:
        ser = open_safe(port)
    except Exception as e:
        print("!! 打开失败：%s（可能被其它软件占用，请先关闭）" % e)
        return

    print("已打开（dtr=False rts=False）。开始计时读 2.0 秒...\n")

    t0 = time.time()
    events = []          # (t, line)
    buf = bytearray()
    last_rx = t0

    while (time.time() - t0) < 2.0:
        chunk = ser.read(128)
        now = time.time()
        if chunk:
            dt = now - t0
            buf.extend(chunk)
            last_rx = now
            txt = bytes(chunk).decode("utf-8", "replace")
            for ln in txt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if ln.strip():
                    events.append((dt, ln.strip()))

    ser.close()

    total = len(buf)
    first = events[0][0] if events else None
    print("-" * 64)
    print("总字节=%d  事件行数=%d  首行时刻=%s" %
          (total, len(events), ("%.3fs" % first) if first is not None else "无"))

    if events:
        print("\n前 12 行（含时刻）：")
        for dt, ln in events[:12]:
            print("   t=%.3fs | %s" % (dt, ln[:70]))

    print("\n" + "-" * 64)
    print("判定：")
    if total == 0:
        print("  [C] 打开后 2 秒零字节 —— 打开动作**没有**产生启动日志。")
        print("      若 MCU 本应周期性输出（心跳/诊断），则说明 MCU 此刻没在跑，")
        print("      或输出根本没经过这条 CH340K 的 TX 通路。")
    elif first is not None and first < 0.5 and ("BL:" in "".join(
            e[1] for e in events) or "main:" in "".join(e[1] for e in events)):
        print("  [A] 打开后 %.3fs 立即出现**启动横幅** —— 打开了就复位了！" % first)
        print("      => 板载 CH340K 的 RTS→NRST 复位脉冲在作祟。")
    else:
        print("  [B] 打开后有数据（%.3fs 起，共 %d 字节）—— 需人工看内容判断。" % (first, total))
    print("=" * 64)


if __name__ == "__main__":
    main()
