#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
串口探测：在给定串口列表上分别发送回车，看哪个口有数据返回，
以此确定"哪个 COM 口对应正在运行固件的那块板"。

用法:
    python probe_ports.py COM5 COM3
    python probe_ports.py COM5 COM3 --baud 115200
"""
import argparse
import sys
import time

try:
    import serial
except ImportError:
    print("需要 pyserial：pip install pyserial")
    sys.exit(1)


def probe(port: str, baud: int, wait_s: float = 1.2) -> bytes:
    """打开串口，发几次回车，收集返回数据。"""
    data = b""
    try:
        with serial.Serial(port, baud, timeout=0.25) as ser:
            ser.reset_input_buffer()
            # 多打几个回车，确保固件已进入命令行
            for _ in range(3):
                ser.write(b"\r\n")
                ser.flush()
                time.sleep(0.15)
            deadline = time.time() + wait_s
            while time.time() < deadline:
                chunk = ser.read(4096)
                if chunk:
                    data += chunk
                    deadline = time.time() + 0.4  # 有新数据就继续等
    except Exception as exc:  # noqa: BLE001
        return ("__ERROR__" + str(exc)).encode()
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ports", nargs="+", help="要探测的串口，如 COM5 COM3")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    print(f"逐个探测 {args.ports} @ {args.baud} 8N1 ...\n")
    for p in args.ports:
        resp = probe(p, args.baud)
        text = resp.decode("utf-8", "replace")
        n = len(resp)
        print(f"--- {p} : 收到 {n} 字节 ---")
        if text.startswith("__ERROR__"):
            print(f"    [打不开] {text[9:]}")
        elif n == 0:
            print("    (静默：无任何返回)")
        else:
            print(text.replace("\r", "\\r").replace("\n", "\\n\n    "))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
