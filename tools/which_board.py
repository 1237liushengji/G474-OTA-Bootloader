#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读 COM3/COM5，打印各口的输出来源指纹，用于区分板子身份。"""
import serial
import serial.tools.list_ports as lp


def probe(port, secs=5.0):
    import time
    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.1
    s.rtscts = False
    s.dsrdtr = False
    s.dtr = False
    s.rts = False
    s.open()
    s.reset_input_buffer()
    d = b""
    t = time.time()
    while time.time() - t < secs:
        c = s.read(4096)
        if c:
            d += c
    s.close()
    return d


def main():
    ports = [p.device for p in lp.comports()]
    print("当前串口:", ports)
    for port in ports:
        try:
            d = probe(port)
        except Exception as exc:
            print(f"{port}: 打不开 {exc}")
            continue
        txt = d.decode("utf-8", "replace")
        fp = "FP_X0" if "FP-X0" in txt else ("FP_B2" if "FP_B2" in txt else "?")
        print(f"{port}: {len(d)} 字节 | FP={fp} | BL={'yes' if 'BL:' in txt else 'no'}"
              f" | uart={'yes' if '[uart]' in txt else 'no'}")


if __name__ == "__main__":
    raise SystemExit(main())
