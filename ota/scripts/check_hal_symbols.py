#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAL/CMSIS 符号存在性检查（无编译器环境下的替代性验证）。

思路：把新增/修改源文件里出现的 `HAL_xxx(...)` 函数调用、`FLASH_xxx` / `GPIO_xxx` /
`IWDG_xxx` / `FDCAN_xxx` 等宏，逐个到工程真实的 HAL 头文件里查找声明/定义。
找不到就报 FAIL —— 这类"符号不存在"的问题在 Keil 里就是编译错误，
本机没有 ARMCC，所以用这种方法提前抓出来。

用法:  python tools_check_hal_symbols.py
"""
import os

from paths import workspace_root
import re
import sys

HAL_INC = "G474/Drivers/STM32G4xx_HAL_Driver/Inc"
CMSIS_INC = "G474/Drivers/CMSIS/Include"
DEV_INC = "G474/Drivers/CMSIS/Device/ST/STM32G4xx/Include"
HAL_SRC = "G474/Drivers/STM32G4xx_HAL_Driver/Src"

# 待检查的源文件
TARGETS = [
    "G474/Drivers/User/Src/ota_agent.c",
    "G474/Drivers/User/Src/ota_tunnel.c",
    "G474/Drivers/User/Src/uds.c",
    "G474/Drivers/User/Src/can.c",
    "G474/Drivers/User/Src/console.c",
    "G474/Core/Src/main.c",
    "G474/Core/Src/system_stm32g4xx.c",
]

# 允许出现在代码里、但由 CMSIS/编译器提供的符号（不查头文件）
IGNORE = {
    "HAL_GetTick",            # 由 HAL 提供，声明在 stm32g4xx_hal.h
    "SCB", "NVIC", "SysTick", "FLASH", "GPIOE", "DBGMCU",
}


def load_header_text():
    chunks = []
    for root in (HAL_INC, CMSIS_INC, DEV_INC):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(".h"):
                    p = os.path.join(dirpath, fn)
                    try:
                        chunks.append(open(p, encoding="utf-8", errors="replace").read())
                    except Exception:
                        pass
    # 源文件里也会有宏定义（少数宏定义在 .c 中）
    if os.path.isdir(HAL_SRC):
        for fn in os.listdir(HAL_SRC):
            if fn.endswith(".c"):
                try:
                    chunks.append(open(os.path.join(HAL_SRC, fn), encoding="utf-8",
                                       errors="replace").read())
                except Exception:
                    pass
    return "\n".join(chunks)


def strip_code(t):
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    t = re.sub(r"//[^\n]*", "", t)
    t = re.sub(r'"(?:\\.|[^"\\])*"', '""', t)
    t = re.sub(r"'(?:\\.|[^'\\])*'", "''", t)
    return t


def main():
    hdr = load_header_text()
    if not hdr:
        print("!! 未找到 HAL 头文件（HAL_INC=%s）" % HAL_INC)
        return 2

    # 收集所有被调用/使用的候选符号
    call_re = re.compile(r"\b((?:HAL|__HAL)_[A-Za-z0-9_]+)\s*\(")
    macro_re = re.compile(r"\b(FLASH_[A-Z0-9_]+|IWDG_[A-Z0-9_]+|FDCAN_[A-Z0-9_]+|"
                          r"GPIO_[A-Za-z0-9_]+|UART_[A-Z0-9_]+|RCC_[A-Z0-9_]+|"
                          r"PWR_[A-Z0-9_]+|__HAL_RCC_[A-Z0-9_]+)\b")

    total_bad = 0
    for f in TARGETS:
        if not os.path.exists(f):
            print("[SKIP] %s 不存在" % f)
            continue
        code = strip_code(open(f, encoding="utf-8", errors="replace").read())
        syms = set(call_re.findall(code)) | set(macro_re.findall(code))
        # 只检查本次改动引入的新符号由调用方关注；这里全量查，但过滤纯本地宏
        bad = []
        for s in sorted(syms):
            if s in IGNORE:
                continue
            # 头文件中出现即认为可用（声明或宏定义）
            if re.search(r"\b" + re.escape(s) + r"\b", hdr):
                continue
            bad.append(s)
        status = "OK" if not bad else "FAIL"
        print("[%s] %s  (检查 %d 个符号)" % (status, f, len(syms)))
        for b in bad:
            print("        未在 HAL/CMSIS 头文件中找到: %s" % b)
        total_bad += len(bad)

    print("\n" + "=" * 70)
    if total_bad == 0:
        print("全部符号均可在工程 HAL/CMSIS 头文件中找到 -> 通过")
    else:
        print("共 %d 个符号未找到 -> 需修正（这在 Keil 里会是编译错误）" % total_bad)
    print("=" * 70)
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
