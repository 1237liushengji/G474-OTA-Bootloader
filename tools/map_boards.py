#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通过 SWD 分别连两块 ST-Link，读 UID + 当前运行镜像的指纹，从而把
「ST-Link SN  <->  哪块板  <->  哪个 COM 口  <->  跑的哪版固件」钉死。

关键：每次连接都会 SWrst，所以只能读"复位后立刻能读到的东西"，这里读
  - UID (0x1FFF7590, 96bit)
  - Flash 里的字符串指纹（用 -r32 dump 一段，再在本地找 S1/S2/D2）

用法:
    python tools/map_boards.py
"""
import subprocess
import sys

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"

PROBES = [
    ("8", "COM3? / 板1"),
    ("49FF6D068065575312232167", "COM5? / 板2"),
]


def run(args, timeout=90):
    r = subprocess.run([CLI] + args, capture_output=True, timeout=timeout)
    return r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def read_uid(sn):
    out, _ = run(["-c", f"port=SWD sn={sn} mode=UR", "-r32", "0x1FFF7590", "0x0C"])
    words = []
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("0x1FFF7"):
            parts = ln.split()
            for p in parts[1:]:
                if p.startswith("0x"):
                    try:
                        words.append(int(p, 16))
                    except ValueError:
                        pass
    return words


def main():
    print("=== 用 ST-Link SN 逐个连，读 UID ===")
    for sn, guess in PROBES:
        print(f"\n--- ST-Link SN={sn}  ({guess}) ---")
        try:
            w = read_uid(sn)
        except Exception as exc:  # noqa: BLE001
            print("   连接失败:", exc)
            continue
        if not w:
            print("   UID 读取失败")
        else:
            uid = "".join(f"{x:08X}" for x in w[:3])
            print(f"   UID = {uid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
