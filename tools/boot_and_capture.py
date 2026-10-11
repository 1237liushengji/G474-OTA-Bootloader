#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一体化冷启动抓取：先开串口监听线程，再用 SWD 复位目标板（UR 模式，
复位后 CLI 一退出就释放 SWD，内核自由运行），捕获完整启动输出。

关键点：
  * 必须先开串口再复位 —— 横幅只打一次，晚开就错过。
  * mode=UR 的 halt 只持续到 CLI 退出；退出后内核跑起来，串口就有输出。

用法:
    python boot_and_capture.py --port COM3 --sn 8 --secs 12
"""
import argparse
import subprocess
import sys
import threading
import time

try:
    import serial
except ImportError:
    print("需要 pyserial：pip install pyserial")
    sys.exit(1)

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--sn", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--secs", type=float, default=12.0)
    args = ap.parse_args()

    chunks = []
    stop = threading.Event()

    with serial.Serial(args.port, args.baud, timeout=0.15) as ser:
        # ⚠️ 必须放开 DTR/RTS（pyserial 默认拉高，CH340 板子会因此被按在复位上）
        try:
            ser.dtr = False
            ser.rts = False
        except Exception:  # noqa: BLE001
            pass
        ser.reset_input_buffer()

        def reader():
            while not stop.is_set():
                try:
                    d = ser.read(4096)
                except Exception:  # noqa: BLE001
                    break
                if d:
                    chunks.append(d)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        print(f"[*] 串口 {args.port} 已就绪，正在复位目标 (SN={args.sn}) ...", flush=True)
        r = subprocess.run(
            [CLI, "-c", f"port=SWD sn={args.sn} mode=UR", "-rst"],
            capture_output=True, text=True, timeout=90,
        )
        tail = [ln for ln in r.stdout.splitlines() if "Reset" in ln or "Error" in ln]
        for ln in tail:
            print("    " + ln.strip())

        time.sleep(args.secs)
        stop.set()
        t.join(timeout=1.0)

    data = b"".join(chunks)
    print(f"\n=== 收到 {len(data)} 字节 ===")
    if data:
        print(data.decode("utf-8", "replace").replace("\r", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
