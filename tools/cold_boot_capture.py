#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯串口冷启动抓取（不使用 SWD，不干扰内核）。

用途：判断"板子上电后到底打不打印"。
关键背景（项目文档已定性）：
  * CubeProgrammer 的 mode=UR / mode=normal / hotplug 连接都会把内核 **halt**，
    读到的寄存器是复位默认值，不是运行态 —— 用它判断"在不在跑"必然出错。
  * 正确做法：断开调试器，冷启动，纯串口监听。

用法:
    python cold_boot_capture.py --port COM3 --secs 15
    python cold_boot_capture.py --all --secs 15        # 同时听所有串口
提示：运行本脚本后，请给板子【断电 3 秒 → 上电】。
"""
import argparse
import sys
import threading
import time

try:
    import serial
    import serial.tools.list_ports as lp
except ImportError:
    print("需要 pyserial：pip install pyserial")
    sys.exit(1)


def monitor(port: str, baud: int, dur: float, out: list, lock: threading.Lock):
    data = b""
    try:
        with serial.Serial(port, baud, timeout=0.2) as ser:
            # ⚠️ 关键：pyserial 打开端口默认把 DTR/RTS 拉高，而 CH340 小板常把它们
            # 接到目标板 NRST/BOOT0 → "打开端口即把板子按在复位上"，一个字节都收不到。
            # 必须显式放开（见 ota/docs/12 §3，实测踩过）。
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:  # noqa: BLE001
                pass
            ser.reset_input_buffer()
            t_end = time.time() + dur
            while time.time() < t_end:
                chunk = ser.read(4096)
                if chunk:
                    data += chunk
    except Exception as exc:  # noqa: BLE001
        with lock:
            print(f"[{port}] 打不开: {exc}")
        return
    with lock:
        out.append((port, data))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="指定串口")
    ap.add_argument("--all", action="store_true", help="监听所有在线串口")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--secs", type=float, default=15.0)
    args = ap.parse_args()

    if args.all:
        ports = [p.device for p in lp.comports()]
    elif args.port:
        ports = [args.port]
    else:
        print("请指定 --port 或 --all")
        return 2

    print(f"=== 监听 {ports} @ {args.baud}，持续 {args.secs}s ===")
    print(">>> 现在请给板子【断电 3 秒 → 上电】 <<<\n")

    out: list = []
    lock = threading.Lock()
    threads = [
        threading.Thread(target=monitor, args=(p, args.baud, args.secs, out, lock), daemon=True)
        for p in ports
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("\n=== 结果 ===")
    for p, data in sorted(out, key=lambda x: -len(x[1])):
        print(f"--- {p} : {len(data)} 字节 ---")
        if data:
            print(data.decode("utf-8", "replace").replace("\r", ""))
        else:
            print("(静默)")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
