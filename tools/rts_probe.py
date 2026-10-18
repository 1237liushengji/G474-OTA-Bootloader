#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rts_probe.py —— 分别测试 RTS/DTR 各组合下 write() 是否导致复位。

矩阵：
  (dtr=False, rts=False)  基线（已知会复位）
  (dtr=True,  rts=True )  都拉低(True=激活)
  (dtr=True,  rts=False)
  (dtr=False, rts=True )

每次测试：发送 6 秒，统计 BL 横幅次数（=复位次数）。
另外：**每次 write 前后都显式重设 RTS/DTR**，防止驱动自动改变。

用法：python tools/rts_probe.py COM3
"""
import sys
import time
import threading

try:
    import serial
except ImportError:
    print("需要 pyserial"); sys.exit(1)


def run_case(port, dtr, rts, secs=6.0):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = 0.02
    ser.write_timeout = 0.5
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = dtr
    ser.rts = rts
    ser.open()
    # 打开后再次强制设定
    ser.dtr = dtr
    ser.rts = rts

    stop = threading.Event()
    tx = [0]

    def sender():
        while not stop.is_set():
            try:
                ser.dtr = dtr      # 每次写前强制
                ser.rts = rts
                ser.write(b"U" * 20)
                tx[0] += 20
            except Exception:
                pass
            time.sleep(0.002)

    th = threading.Thread(target=sender, daemon=True)
    th.start()
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < secs:
        c = ser.read(1024)
        if c:
            buf.extend(c)
    stop.set()
    th.join(timeout=0.3)
    ser.close()

    n = buf.decode("utf-8", "replace").count("BL: clock HSE+PLL")
    return tx[0], len(buf), n


port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
print("=" * 66)
print("rts_probe：%s —— DTR/RTS 组合对复位的影响" % port)
print("=" * 66)

cases = [
    ("dtr=False rts=False", False, False),
    ("dtr=True  rts=True ", True,  True),
    ("dtr=True  rts=False", True,  False),
    ("dtr=False rts=True ", False, True),
]

results = []
for name, dtr, rts in cases:
    try:
        tx, rx, n = run_case(port, dtr, rts)
        results.append((name, tx, rx, n))
        print("  %s : 发送 %6d B, 收到 %5d B, 复位 %d 次" % (name, tx, rx, n))
    except Exception as e:
        print("  %s : 异常 %s" % (name, e))

print()
print("-" * 66)
print("结论：")
for name, tx, rx, n in results:
    tag = "复位!" if n > 0 else "稳定"
    print("  %s -> %s" % (name, tag))
print("-" * 66)
print("若只有 rts=True 的用例复位 => RTS->NRST 是元凶")
print("若所有用例都复位      => 与 RTS/DTR 无关（信号/电源耦合）")
print("=" * 66)
