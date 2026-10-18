#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯读取抓取启动日志：open_safe() 打开后只 read、绝不 write
（write 会经 CH340 一键下载电路复位整板，污染观测）。

用法:
    python tools/quiet_capture.py --port COM3 --secs 25
    python tools/quiet_capture.py --port COM5 --secs 25
"""
import argparse
import sys
import time

import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serial_safe import open_safe   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--secs", type=float, default=25.0)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    ser = open_safe(args.port, args.baud)
    print(f"[*] {args.port} opened (dtr/rts already low). reading {args.secs}s ...")
    t0 = time.time()
    total = 0
    buf = bytearray()
    while (time.time() - t0) < args.secs:
        try:
            data = ser.read(4096)
        except Exception as e:  # noqa: BLE001
            print(f"[!] read error: {e}")
            break
        if data:
            total += len(data)
            buf += data
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()
    ser.close()
    print(f"\n[*] done. total={total} bytes in {args.secs}s")


if __name__ == "__main__":
    raise SystemExit(main())
