#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
串口插拔实时监视器。

用途：板上两个 USB-C 口（左=USB转串口 / 右=USB设备），要搞清
      「哪个物理口 对应 哪个 COM」，最可靠的办法就是：
      开着这个脚本 → 拔/插一根线 → 看 COM 列表怎么变。

用法:
    python tools/watch_ports.py --secs 60
"""
import argparse
import time

import serial.tools.list_ports as lp


def snapshot():
    out = {}
    for p in lp.comports():
        out[p.device] = (p.location or "", p.description or "", p.serial_number or "")
    return out


def fmt(d):
    if not d:
        return "  (无串口)"
    return "\n".join(
        f"  {dev:6s}  loc={loc:10s}  ser={ser!r:12s}  {desc}"
        for dev, (loc, desc, ser) in sorted(d.items())
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    print(f"=== 串口插拔监视 {args.secs:.0f}s（每 {args.interval}s 扫一次）===")
    print(">>> 现在请拔掉/插上那根线，观察下面的变化 <<<\n", flush=True)

    prev = snapshot()
    print(f"[t=0.0s] 初始：\n{fmt(prev)}\n", flush=True)

    t0 = time.time()
    n = 0
    while time.time() - t0 < args.secs:
        time.sleep(args.interval)
        n += 1
        cur = snapshot()
        if cur != prev:
            added = set(cur) - set(prev)
            removed = set(prev) - set(cur)
            stamp = time.time() - t0
            print(f"[t={stamp:5.1f}s] *** 变化 ***", flush=True)
            for d in sorted(added):
                print(f"    + 新增 {d}  loc={cur[d][0]}  {cur[d][1]}", flush=True)
            for d in sorted(removed):
                print(f"    - 消失 {d}  loc={prev[d][0]}  {prev[d][1]}", flush=True)
            if not added and not removed:
                print("    (属性变化)", flush=True)
                for d in sorted(cur):
                    if cur[d] != prev.get(d):
                        print(f"    ~ {d}: {prev.get(d)} -> {cur[d]}", flush=True)
            print(flush=True)
            prev = cur

    print(f"\n=== 最终 ===\n{fmt(snapshot())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
