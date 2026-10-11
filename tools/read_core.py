#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取目标板核心寄存器快照，判定固件当前在做什么：
  - 连不上 / 连上后读不了寄存器  -> 总线挂死（与之前 TXBAR 挂死同型）
  - PC 落在 0x0800xxxx          -> 正在跑 Flash 里的固件
  - PC 落在 0x1FFFxxxx / 别的   -> 在 ROM bootloader 或异常态

用法:
    python read_core.py --sn 8
"""
import argparse
import re
import subprocess

CLI = r"D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"

# 通过 -r32 读 Cortex-M 调试寄存器：
#   0xE000ED04 ICSR, 0xE000ED28 CFSR, 0xE000ED2C HFSR,
#   0xE000ED34 MMFAR, 0xE000ED38 BFAR, 0xE000ED24 SHCSR
REGS = [
    (0xE000ED04, "ICSR "),
    (0xE000ED24, "SHCSR"),
    (0xE000ED28, "CFSR "),
    (0xE000ED2C, "HFSR "),
    (0xE000ED34, "MMFAR"),
    (0xE000ED38, "BFAR "),
]


def rd32(sn: str, addr: int, nbytes: int = 4) -> str:
    r = subprocess.run(
        [CLI, "-c", f"port=SWD sn={sn} mode=hotplug", "-r32", hex(addr), str(nbytes)],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    args = ap.parse_args()

    print(f"=== 板 SN={args.sn} 核心寄存器快照 ===")
    for addr, name in REGS:
        out = rd32(args.sn, addr)
        m = re.search(r"0x[0-9A-Fa-f]{8}\s*:\s*([0-9A-Fa-f ]+)", out)
        val = m.group(1).strip() if m else "??"
        err = "  <-- 读失败!" if not m else ""
        print(f"  {name} @ 0x{addr:08X} = {val}{err}")
        if not m and "Error" in out:
            for ln in out.splitlines():
                if "Error" in ln:
                    print(f"      {ln.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
