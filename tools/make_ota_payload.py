#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按"目标槽"构建并内嵌 OTA payload
================================================================
背景 / 为什么需要这个脚本
    STM32G4 的 APP 不是位置无关代码：Slot A 链接在 0x0800C080、
    Slot B 链接在 0x08040880，相差 0x34800。同一份二进制不可能在两个槽
    都能跑（BL 的 RESET_VEC 校验会直接拒绝落在槽外的复位向量）。
    因此"刷哪个槽"就必须用**该槽链接**的镜像。

    为什么不做双镜像同时内嵌：
    一份 payload ≈ 79.6KB，两份 + APP 本体 = 238KB > OTA_SLOT_SIZE(208KB)，
    装不下（实测差 25.5KB）。故改为"构建时选择目标槽"，
    换槽升级时重跑本脚本即可（一条命令）。

用法
    # 生成"用于刷 Slot B"的镜像并嵌入板A，然后重建板A
    python tools/make_ota_payload.py --target-slot B --version 8.2

    # 生成"用于刷 Slot A"的镜像（板B 在跑 Slot B 时用它换回 A）
    python tools/make_ota_payload.py --target-slot A --version 8.2

    # 只生成 payload 与 .c，不自动重建板A
    python tools/make_ota_payload.py --target-slot B --version 8.2 --no-rebuild

注意
    * 默认 payload 角色 = 对端(ECU)角色 = CAN_NODE_B / OTA_CLIENT_ENABLE=0
    * 脚本会临时改写 G474.uvprojx，结束时**必定还原**（含异常路径）
    * 产物：
        tmp/OTA_payload_slot{SLOT}_factory.bin   —— 带 32B 头的完整镜像
        G474/Drivers/User/Src/ota_image_data.c   —— 内嵌数组（供板A 使用）
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJ = os.path.join(ROOT, "G474", "MDK-ARM", "G474.uvprojx")
PROJ_BAK = PROJ + ".slotbak"
HEX_IN = os.path.join(ROOT, "G474", "MDK-ARM", "G474", "G474.hex")
UV4 = r"D:/U5/UV4/UV4.exe"
PY = sys.executable

# 槽 -> (IROM1 起始, VECT_TAB_OFFSET)
SLOT_MAP = {
    "A": ("0x800c080", "0xC080"),
    "B": ("0x8040880", "0x40880"),
}
# 角色 -> (CAN_NODE_ROLE, APP_BOARD_FP, OTA_CLIENT_ENABLE)
ROLE_MAP = {
    "A": ("CAN_NODE_A", "FP_A1", "1"),
    "B": ("CAN_NODE_B", "FP_B2", "0"),
}


def die(msg):
    print("[ERR] " + msg)
    sys.exit(1)


def run(cmd, cwd=None):
    print("[RUN] " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        die("命令失败（returncode=%d）" % r.returncode)
    return r.stdout


def patch_project(slot, role):
    src = io.open(PROJ, encoding="utf-8").read()
    irom, vec = SLOT_MAP[slot]
    node, fp, cli = ROLE_MAP[role]

    # 1) 宏定义：角色 / 指纹 / 客户端 / 向量表偏移
    def repl_define(m):
        items = [x for x in m.group(1).split(",")]
        out = []
        for it in items:
            if it.startswith("CAN_NODE_ROLE="):
                it = "CAN_NODE_ROLE=" + node
            elif it.startswith("APP_BOARD_FP="):
                it = "APP_BOARD_FP=" + fp
            elif it.startswith("OTA_CLIENT_ENABLE="):
                it = "OTA_CLIENT_ENABLE=" + cli
            elif it.startswith("VECT_TAB_OFFSET="):
                it = "VECT_TAB_OFFSET=" + vec
            out.append(it)
        return "<Define>" + ",".join(out) + "</Define>"

    src, n = re.subn(r"<Define>((?:[^<]*?CAN_NODE_ROLE=[^<]*?))</Define>",
                     repl_define, src)
    if n != 1:
        die("未能唯一定位 <Define>（匹配到 %d 处）" % n)

    # 2) IROM1 起始地址（OCR_RVCT4 即 IROM1）
    src, n = re.subn(r"(<OCR_RVCT4>\s*<Type>1</Type>\s*<StartAddress>)0x[0-9a-fA-F]+(</StartAddress>)",
                     lambda m: m.group(1) + irom + m.group(2), src)
    if n != 1:
        die("未能唯一定位 OCR_RVCT4.StartAddress（匹配到 %d 处）" % n)

    io.open(PROJ, "w", encoding="utf-8").write(src)
    print("[OK ] uvprojx 已切换到 slot=%s role=%s (IROM1=%s, VECT_TAB_OFFSET=%s)"
          % (slot, role, irom, vec))


def build():
    log = os.path.join(ROOT, "G474", "MDK-ARM", "build_payload.log")
    subprocess.run([UV4, "-r", "G474.uvprojx", "-j0", "-o", "build_payload.log"],
                   cwd=os.path.join(ROOT, "G474", "MDK-ARM"),
                   capture_output=True, text=True)
    txt = io.open(log, encoding="utf-8", errors="ignore").read() if os.path.isfile(log) else ""
    if "0 Error(s)" not in txt:
        print(txt[-3000:])
        die("Keil 编译失败")
    m = re.search(r"Program Size:\s*(.+)", txt)
    print("[OK ] 编译通过  " + (m.group(1) if m else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-slot", required=True, choices=["A", "B"],
                    help="payload 要被刷到哪个槽")
    ap.add_argument("--role", default="B", choices=["A", "B"],
                    help="payload 的角色（对端是 ECU，默认 B）")
    ap.add_argument("--version", default="8.2", help="镜像版本号（写进 32B 头）")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="只生成 payload 与 .c，不重建板A")
    args = ap.parse_args()

    if not os.path.isfile(PROJ):
        die("找不到工程文件: " + PROJ)
    shutil.copy2(PROJ, PROJ_BAK)
    try:
        patch_project(args.target_slot, args.role)
        build()

        if not os.path.isfile(HEX_IN):
            die("找不到编译产物: " + HEX_IN)
        raw_hex = os.path.join(ROOT, "tmp", "OTA_payload_slot%s_raw.hex" % args.target_slot)
        os.makedirs(os.path.dirname(raw_hex), exist_ok=True)
        shutil.copy2(HEX_IN, raw_hex)

        run([PY, os.path.join(ROOT, "ota", "scripts", "make_factory_image.py"),
             "--hex", raw_hex, "--slot", args.target_slot,
             "--version", args.version], cwd=ROOT)

        src_bin = raw_hex.replace("_raw.hex", "_raw_factory.bin")
        dst_bin = os.path.join(ROOT, "tmp", "OTA_payload_slot%s_factory.bin" % args.target_slot)
        if not os.path.isfile(src_bin):
            die("找不到 factory bin: " + src_bin)
        shutil.copy2(src_bin, dst_bin)

        run([PY, os.path.join(ROOT, "ota", "scripts", "gen_ota_image_c.py"),
             "--bin", dst_bin,
             "--out", os.path.join(ROOT, "G474", "Drivers", "User", "Src", "ota_image_data.c"),
             "--sym", "g_otaImage"], cwd=ROOT)
    finally:
        shutil.copy2(PROJ_BAK, PROJ)
        os.remove(PROJ_BAK)
        print("[OK ] uvprojx 已还原（Slot A / role A）")

    if not args.no_rebuild:
        print("\n[..] 重建板A（内嵌刚生成的 payload）...")
        log = os.path.join(ROOT, "G474", "MDK-ARM", "build_a.log")
        subprocess.run([UV4, "-r", "G474.uvprojx", "-j0", "-o", "build_a.log"],
                       cwd=os.path.join(ROOT, "G474", "MDK-ARM"),
                       capture_output=True, text=True)
        txt = io.open(log, encoding="utf-8", errors="ignore").read() if os.path.isfile(log) else ""
        if "0 Error(s)" not in txt:
            print(txt[-3000:])
            die("板A 重建失败")
        m = re.search(r"Program Size:\s*(.+)", txt)
        print("[OK ] 板A 重建通过  " + (m.group(1) if m else ""))
        print("\n下一步：烧录板A 的 factory 镜像")
        print("  python ota/scripts/make_factory_image.py --hex tmp/APP_A_raw.hex --slot A --version <v>")

    print("\n[DONE] payload 目标槽 = Slot %s，角色 = %s" % (args.target_slot, args.role))


if __name__ == "__main__":
    main()
