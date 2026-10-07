#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 Bootloader 生成独立的 Keil 工程：ota/bootloader/MDK-ARM/BL.uvprojx

做法：从已验证可编译的 APP 工程 G474/MDK-ARM/G474.uvprojx 派生，
      —— 保留 HAL 驱动 / CMSIS / 启动文件（BL 与 APP 共用同一套芯片支持）
      —— 删掉 APP 侧源码组（Core 的 main/gpio/it/msp/system、Drivers/User 全部）
      —— 换成 ota/bootloader/Core/Src 的 11 个源文件
      —— 链接配置改为 scatter 文件 bl.sct（向量表 0x08000000 + 代码 0x08000800）
这样能用最小改动得到"与 APP 同构、但独立"的 BL 工程，避免手写整个 uvprojx。
"""
import os

from paths import workspace_root
from bl_layout import BL_REGION_SIZE, SCATTER_TEXT as SCATTER   # 布局单一真相源（页0只放向量表）
import re
import shutil
import sys

ROOT = workspace_root()
BL = os.path.join(ROOT, "ota", "bootloader")
BL_MDK = os.path.join(BL, "MDK-ARM")
APP_PROJ = os.path.join(ROOT, "G474", "MDK-ARM", "G474.uvprojx")
APP_STARTUP = os.path.join(ROOT, "G474", "MDK-ARM", "startup_stm32g474xx.s")

BL_SRC = ["main.c", "bl_clock.c", "bl_can.c", "bl_flash_if.c", "bl_crc32.c",
          "bl_param.c", "bl_image.c", "bl_jump.c", "bl_isotp.c", "bl_diag.c", "bl_uart.c"]

# BL 实际需要的 HAL 模块（其余一律不编译，显著减小体积）。
# 依据：bl_uart.c 直接操作 USART1 寄存器、不用 HAL_UART；
#       bl_clock.c 只用 RCC/PWR；bl_can.c 只用 FDCAN；bl_flash_if.c 只用 FLASH；
#       main.c 只用 IWDG 与 Cortex 核心函数。
# 被剔除的 9 个（tim/tim_ex/dma/dma_ex/exti/gpio/uart/uart_ex/flash_ramfunc）
# 在旧版本里合计占用约 4~6KB，但对 BL 没有任何调用者。
BL_KEEP_HAL = {
    "stm32g4xx_hal.c",
    "stm32g4xx_hal_cortex.c",
    "stm32g4xx_hal_dma.c",      # HAL_UART_DeInit -> HAL_DMA_Abort，必须有
    "stm32g4xx_hal_fdcan.c",
    "stm32g4xx_hal_flash.c",
    "stm32g4xx_hal_flash_ex.c",
    "stm32g4xx_hal_gpio.c",     # bl_can.c 用 HAL_GPIO_Init 配 PB12/PB13
    "stm32g4xx_hal_iwdg.c",
    "stm32g4xx_hal_pwr.c",
    "stm32g4xx_hal_pwr_ex.c",
    "stm32g4xx_hal_rcc.c",
    "stm32g4xx_hal_rcc_ex.c",
    "stm32g4xx_hal_uart.c",     # bl_uart.c 用 HAL_UART_Init/Transmit/DeInit
}

SCATTER = SCATTER       # 兼容旧引用；正文见 ota/scripts/bl_layout.py

DECL = ('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
        '<Project xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="project_projx.xsd">\n')


def main():
    if not os.path.isfile(APP_PROJ):
        print("!! 找不到参考工程: %s" % APP_PROJ)
        return 2

    os.makedirs(BL_MDK, exist_ok=True)

    # 1) 拷贝启动文件（与 APP 同一份，保证芯片支持一致）
    shutil.copy2(APP_STARTUP, os.path.join(BL_MDK, "startup_stm32g474xx.s"))

    # 2) 写 scatter 文件
    with open(os.path.join(BL_MDK, "bl.sct"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(SCATTER)

    raw = open(APP_PROJ, encoding="utf-8", errors="replace").read()

    # 3) 删掉 APP 侧源码组（整段 <Group>...</Group>）
    #    Drivers/User   -> 整组删除
    #    Application/User/Core -> 整组删除（main/gpio/it/msp）
    #    Drivers/CMSIS  -> 整组删除（其唯一文件 system_stm32g4xx.c 由 BL 的 bl_clock.c 取代）
    def drop_group(text, group_name):
        pat = re.compile(
            r"[ \t]*<Group>\s*<GroupName>" + re.escape(group_name) +
            r"</GroupName>.*?</Group>\s*", re.S)
        new, n = pat.subn("", text)
        print("   删除组 %-28s 命中 %d" % (group_name, n))
        if n != 1:
            raise SystemExit("!! 组 %s 未唯一匹配（%d）" % (group_name, n))
        return new

    for g in ("Drivers/User", "Application/User/Core", "Drivers/CMSIS"):
        raw = drop_group(raw, g)

    # 4) 把 Application/MDK-ARM 组改名为 Bootloader，并挂上 11 个 BL 源文件
    startup_entry = ("            <File>\n"
                     "              <FileName>startup_stm32g474xx.s</FileName>\n"
                     "              <FileType>2</FileType>\n"
                     "              <FilePath>startup_stm32g474xx.s</FilePath>\n"
                     "            </File>")
    if raw.count(startup_entry) != 1:
        # 缩进可能不同，退化为按 FileName 定位
        m = re.search(r"[ \t]*<File>\s*<FileName>startup_stm32g474xx\.s</FileName>.*?</File>", raw, re.S)
        if not m:
            raise SystemExit("!! 未找到 startup 文件条目")
        startup_entry = m.group(0)
        print("   注意：startup 条目缩进与预期不同，已按正则定位")

    bl_files = "\n".join(
        "            <File>\n"
        "              <FileName>%s</FileName>\n"
        "              <FileType>1</FileType>\n"
        "              <FilePath>..\\Core\\Src\\%s</FilePath>\n"
        "            </File>" % (f, f) for f in BL_SRC)

    raw = raw.replace(startup_entry, startup_entry + "\n" + bl_files, 1)
    raw = raw.replace("<GroupName>Application/MDK-ARM</GroupName>",
                      "<GroupName>Bootloader</GroupName>", 1)

    # 4c) 加回 system_stm32g4xx.c —— 它提供 SystemInit / SystemCoreClock /
    #     AHBPrescTable / APBPrescTable（启动文件与 HAL RCC/PWR 都依赖），
    #     其 VTOR 偏移已改为由工程宏 VECT_TAB_OFFSET 提供，因此 BL/APP 共用同一份文件、互不干扰。
    sys_entry = ("            <File>\n"
                 "              <FileName>system_stm32g4xx.c</FileName>\n"
                 "              <FileType>1</FileType>\n"
                 "              <FilePath>../../../G474/Core/Src/system_stm32g4xx.c</FilePath>\n"
                 "            </File>")
    anchor = ("            <File>\n"
              "              <FileName>bl_uart.c</FileName>\n"
              "              <FileType>1</FileType>\n"
              "              <FilePath>..\\Core\\Src\\bl_uart.c</FilePath>\n"
              "            </File>")
    if raw.count(anchor) != 1:
        raise SystemExit("!! 未找到 bl_uart.c 锚点（用于插入 system_stm32g4xx.c）")
    raw = raw.replace(anchor, anchor + "\n" + sys_entry, 1)
    print("   已加回 system_stm32g4xx.c（提供 SystemInit 等链接符号）")

    # 4d) 编译器宏：BL 的向量表偏移为 0（与 APP 的 0x8020 区分开）。
    #     用正则匹配，兼容"已含宏"与"原始"两种形态，脚本可重复运行。
    new_def = ("<Define>USE_HAL_DRIVER,STM32G474xx,"
               "USER_VECT_TAB_ADDRESS,VECT_TAB_OFFSET=0x0</Define>")
    raw, n_def = re.subn(r"<Define>USE_HAL_DRIVER,STM32G474xx[^<]*</Define>", new_def, raw)
    print("   设置 Define 处数 = %d" % n_def)
    if n_def != 1:
        raise SystemExit("!! Define 匹配 %d 次（期望 1）" % n_def)
    #     原 "..\Drivers\..." 解析不到，需改为 "../../../G474/Drivers/..."。
    raw, n_hal = re.subn(r"<FilePath>(\.\.[\\/]Drivers[\\/][^<]*)</FilePath>",
                         lambda m: "<FilePath>../../../G474/" + m.group(1)[3:].replace("\\", "/") + "</FilePath>",
                         raw)
    print("   重写 HAL 源文件路径处数 = %d" % n_hal)
    if n_hal == 0:
        raise SystemExit("!! 未重写任何 HAL 路径，BL 工程会找不到 HAL 源文件")

    # 4b-2) 剔除 BL 用不到的 HAL 模块（体积极大且无调用者）
    removed = []

    def drop_file(text, fname):
        pat = re.compile(r"[ \t]*<File>\s*<FileName>" + re.escape(fname) +
                         r"</FileName>.*?</File>\s*", re.S)
        new, n = pat.subn("", text)
        return new, n

    for fn in sorted(re.findall(r"<FileName>(stm32g4xx_hal_[a-z_0-9]+\.c)</FileName>", raw)):
        if fn not in BL_KEEP_HAL:
            raw, n = drop_file(raw, fn)
            if n == 1:
                removed.append(fn)
    print("   剔除 BL 不需要的 HAL 模块 %d 个: %s" % (len(removed), ", ".join(removed) or "(无)"))
    if len(removed) == 0:
        print("   注意：未剔除任何模块（可能已剔除过）")

    # 5) 头文件搜索路径：BL 自己的 Inc + HAL/CMSIS + 芯片配置头所在目录
    #    注意 uvprojx 里有 3 处 <IncludePath>（C / Asm / 另一配置），必须全部改写，
    #    否则某些编译器分支会拿不到路径。
    #    G474/Core/Inc 必须包含：stm32g4xx_hal.h 依赖 stm32g4xx_hal_conf.h，它在那里。
    new_inc = ("<IncludePath>../Core/Inc;"
               "../../../G474/Core/Inc;"
               "../../../G474/Drivers/STM32G4xx_HAL_Driver/Inc;"
               "../../../G474/Drivers/STM32G4xx_HAL_Driver/Inc/Legacy;"
               "../../../G474/Drivers/CMSIS/Device/ST/STM32G4xx/Include;"
               "../../../G474/Drivers/CMSIS/Include</IncludePath>")
    raw, n_inc = re.subn(r"<IncludePath>[^<]*</IncludePath>", new_inc, raw)
    print("   改写 IncludePath 处数 = %d" % n_inc)
    if n_inc == 0:
        raise SystemExit("!! 未找到 IncludePath")

    # 6) 链接配置：BL 从 0x08000000 起。
    #    重要：Keil 默认用 <TargetOption> 里的 IROM/IRAM 生成分散加载文件，
    #    <ScatterFile> 会被忽略 —— 除非显式关闭"使用 Target 对话框内存布局"。
    #    因此这里两件事都做：① 显式指定 bl.sct；② 关闭 Target 内存布局；③ 仍把 IROM 写成正确值，
    #    这样万一有人把开关打开，地址也是对的。
    raw = re.sub(r"<ScatterFile>[^<]*</ScatterFile>",
                 "<ScatterFile>bl.sct</ScatterFile>", raw, count=1)
    # 标签名是 <umfTarg>：1=按 Target 对话框的 IROM/IRAM 重新生成分散加载文件（会使 bl.sct 失效），
    # 0=使用 ScatterFile。早期脚本误写成 <UseMemoryLayoutFromTargetDialog>，那个标签不存在，
    # 于是开关一直是 1、bl.sct 从未生效 —— 这是 2026-10-05 变砖事故的一半原因。
    raw, n_umf = re.subn(r"<umfTarg>\s*[01]\s*</umfTarg>", "<umfTarg>0</umfTarg>", raw)
    if n_umf == 0:
        raw = raw.replace("<ScatterFile>",
                          "<umfTarg>0</umfTarg>\n              <ScatterFile>", 1)
    print("   umfTarg -> 0 处数 = %d（必须为 0，否则 bl.sct 被忽略）" % max(n_umf, 1))

    # IROM / OCR_RVCT4：必须与 bl.sct 的**加载域**一致 = 0x08000000 / 0x0000B800
    REGION = '0x%08X' % BL_REGION_SIZE
    raw, n_irom = re.subn(
        r"(<IROM>\s*<Type>1</Type>\s*<StartAddress>)(0x[0-9A-Fa-f]+)(</StartAddress>\s*<Size>)"
        r"(0x[0-9A-Fa-f]+)(</Size>)",
        r"\g<1>0x8000000\g<3>" + REGION + r"\g<5>", raw)
    raw, n_rvct = re.subn(
        r"(<OCR_RVCT4>\s*<Type>1</Type>\s*<StartAddress>)(0x[0-9A-Fa-f]+)(</StartAddress>\s*<Size>)"
        r"(0x[0-9A-Fa-f]+)(</Size>)",
        r"\g<1>0x8000000\g<3>" + REGION + r"\g<5>", raw)
    print("   IROM 改写 %d 处, OCR_RVCT4 改写 %d 处" % (n_irom, n_rvct))
    if n_irom == 0:
        raise SystemExit("!! 未改写 IROM，BL 会被链接到 APP 的槽地址")

    # 7) 输出名 BL（便于与 APP 的 G474.axf/hex 区分）
    raw = re.sub(r"<OutputName>[^<]*</OutputName>", "<OutputName>BL</OutputName>", raw, count=1)
    if "<OutputName>BL</OutputName>" not in raw:
        raise SystemExit("!! 未设置 OutputName")

    out = os.path.join(BL_MDK, "BL.uvprojx")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(raw)
    print("\n已生成: %s (%d bytes)" % (out, len(raw)))
    print("启动文件: %s" % os.path.join(BL_MDK, "startup_stm32g474xx.s"))
    print("scatter : %s" % os.path.join(BL_MDK, "bl.sct"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
