#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双板纯串口长期抓取（完全不动 SWD）。

目的：解决"上下文中那个自相矛盾" ——
  固件 hex 里已经没有 S1/S2 字符串了，串口日志却还在打印 S1/S2。
  唯一可信的判定方式：给两块板都开一个长时间窗、且全程不碰 SWD，
  让内核自由跑，看看到底谁在说什么、周期是多少。

用法:
    python tools/dual_capture.py --secs 30 --out logs/dual_now.txt
"""
import argparse
import threading
import time
from collections import Counter

import serial
import serial.tools.list_ports as lp


def monitor(port, baud, dur, out, lock):
    data = b""
    err = None
    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = baud
        ser.timeout = 0.05
        ser.rtscts = False
        ser.dsrdtr = False
        ser.dtr = False          # 必须在 open() 之前设
        ser.rts = False
        ser.open()
    except Exception as exc:      # noqa: BLE001
        with lock:
            print(f"[{port}] 打不开: {exc}")
        return
    try:
        ser.reset_input_buffer()
        t_end = time.time() + dur
        while time.time() < t_end:
            try:
                chunk = ser.read(8192)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                break
            if chunk:
                data += chunk
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass
    with lock:
        out.append((port, data, err))


KEYS = [
    "D2: CAN_SelfTest SKIPPED",
    "S1: before CAN_Configure",
    "S2: after CAN_Configure",
    "S6: after restore",
    "CAN self-test",
    "BL: ",
    "APP: ",
    "main loop",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    ports = [p.device for p in lp.comports()]
    print(f"=== 双板纯串口抓取 {ports} @ {args.baud}，{args.secs}s（全程不碰 SWD）===", flush=True)
    print(">>> 若需冷启动，请在 2 秒内给板子断电再上电 <<<\n", flush=True)

    out = []
    lock = threading.Lock()
    ths = [
        threading.Thread(target=monitor, args=(p, args.baud, args.secs, out, lock), daemon=True)
        for p in ports
    ]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    lines = []
    for port, data, err in sorted(out, key=lambda x: x[0]):
        text = data.decode("utf-8", "replace").replace("\r", "")
        cnt = Counter()
        for k in KEYS:
            cnt[k] = text.count(k)
        lines.append(f"--- {port} : {len(data)} 字节 {'ERR=' + err if err else ''} ---")
        for k in KEYS:
            if cnt[k]:
                lines.append(f"    {cnt[k]:3d} x  {k}")
        lines.append("")
        lines.append(text)
        lines.append("=" * 60)

    report = "\n".join(lines)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
