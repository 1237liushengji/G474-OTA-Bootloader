#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓启动横幅：先打开串口监听，再用 ST-Link 复位目标板，捕获上电/复位后的
完整启动输出。用于判定：串口号是否正确、固件是否在跑、卡在哪一步、RX 是否通。

用法:
    python capture_boot.py --port COM3 --sn 8
    python capture_boot.py --port COM3 --sn 8 --secs 12
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
    ap.add_argument("--port", required=True, help="串口，如 COM3")
    ap.add_argument("--sn", required=True, help="ST-Link 序列号，如 8")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--secs", type=float, default=10.0, help="复位后监听时长")
    args = ap.parse_args()

    chunks = []
    stop = threading.Event()

    def reader(ser):
        while not stop.is_set():
            try:
                data = ser.read(4096)
            except Exception:  # noqa: BLE001
                break
            if data:
                chunks.append(data)
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        ser.reset_input_buffer()
        t = threading.Thread(target=reader, args=(ser,), daemon=True)
        t.start()

        print(f"[*] 已打开 {args.port}，复位目标板 (SN={args.sn}) ...", flush=True)
        # 先连接（hotplug 保持目标运行），再发复位命令，避免 mode=ur 把板子按在复位态
        r = subprocess.run(
            [CLI, "-c", f"port=SWD sn={args.sn} mode=hotplug", "-rst"],
            capture_output=True, text=True, timeout=60,
        )
        if "Error" in r.stdout:
            print("[!] 复位命令输出：")
            print(r.stdout[-800:])
            print(r.stderr[-400:])

        time.sleep(args.secs)
        stop.set()
        t.join(timeout=1.0)

    total = sum(len(c) for c in chunks)
    print(f"\n\n=== 共收到 {total} 字节 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
