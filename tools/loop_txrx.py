#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loop_txrx.py —— 健壮版：自动重连 + 持续发送 + 全程记录诊断行。

特点：
  - 端口消失（拔线）时自动重试打开，不退出
  - 重连成功后持续发送 'AA'
  - 记录所有 [uart] 诊断行，结束后汇总

用法：python tools/loop_txrx.py COM3 60
"""
import re
import sys
import time

try:
    import serial
except ImportError:
    print("需要 pyserial")
    sys.exit(1)


def try_open(port, baud=115200):
    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = baud
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.timeout = 0.02
        ser.write_timeout = 0.3
        ser.xonxoff = False
        ser.rtscts = False
        ser.dsrdtr = False
        ser.dtr = False
        ser.rts = False
        ser.open()
        return ser
    except Exception:
        return None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

    print("=" * 68)
    print("loop_txrx：%s，共 %.0f 秒（自动重连）" % (port, secs))
    print(">>> 现在请【拔掉 USB 线，等 2 秒，再插回】<<<")
    print("=" * 68)

    t0 = time.time()
    ser = None
    n_tx = 0
    last_tx = 0.0
    text = []
    reconnects = 0

    while (time.time() - t0) < secs:
        if ser is None:
            ser = try_open(port)
            if ser is None:
                time.sleep(0.2)
                continue
            reconnects += 1
            print("  [%5.1fs] 端口已打开 #%d" % (time.time() - t0, reconnects))

        now = time.time() - t0
        try:
            if (now - last_tx) >= 0.05:
                ser.write(b"AA")
                n_tx += 1
                last_tx = now
            chunk = ser.read(512)
            if chunk:
                text.append(bytes(chunk).decode("utf-8", "replace"))
        except Exception:
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(0.15)

    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass

    joined = "".join(text)
    print("\n" + "-" * 68)
    print("发送 %d 次；重连 %d 次" % (n_tx, reconnects))

    print("\n---- 收到的全部内容 ----")
    print(joined if joined else "(零字节)")
    print("---- END ----")

    isr = re.findall(r"ISR=([0-9A-Fa-f]+)", joined)
    edges = re.findall(r"edges/20ms=(\d+)", joined)
    irq = re.findall(r"rxIrq=(\d+)", joined)
    cr1 = re.findall(r"CR1=([0-9A-Fa-f]+)", joined)
    moder = re.findall(r"MODER=([0-9A-Fa-f]+)", joined)

    print("\n" + "=" * 68)
    print("汇总：")
    print("  CR1     : %s" % sorted(set(cr1)) if cr1 else "  CR1     : 无")
    print("  ISR     : %s" % list(dict.fromkeys(isr)) if isr else "  ISR     : 无")
    print("  edges   : %s" % sorted(set(edges)) if edges else "  edges   : 无")
    print("  rxIrq   : %s" % sorted(set(irq)) if irq else "  rxIrq   : 无")
    print("  MODER   : %s" % sorted(set(moder)) if moder else "  MODER   : 无")
    print("=" * 68)


if __name__ == "__main__":
    main()
