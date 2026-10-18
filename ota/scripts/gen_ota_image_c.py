#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 factory bin 镜像转成可被 ota_client.c 引用的 const C 数组
=============================================================
为什么需要：
    板载 USB 转串口 RX 方向硬件开路（PA10 零跳变），PC 无法下发命令，
    所以 OTA 必须由板 A 用它自带的 UDS 客户端角色在总线上自己发起。
    而板 A 自己也要"有东西可发"，故把目标镜像编进 .rodata
    （不占 RAM，代价是 Flash 增大：镜像多大就涨多少）。

用法：
    python ota/scripts/gen_ota_image_c.py \
        --bin firmware/G474_B_factory.bin \
        --out G474/Drivers/User/Src/ota_image_data.c \
        --sym g_otaImage

注意：
    * 输入必须是 make_factory_image.py 生成的 *_factory.bin（含 32B 镜像头），
      否则目标板的 BL_ImageValidate() / UDS 0x34 校验会拒绝。
    * 单个 chunk 走 ISO-TP 多帧，故数组本身不受单帧长度限制。
"""
import argparse
import os
import sys


def emit_bytes(data: bytes, f, per_line: int = 16) -> None:
    for i in range(0, len(data), per_line):
        chunk = data[i:i + per_line]
        f.write("  " + " ".join("0x%02XU," % b for b in chunk) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, help="factory bin 路径")
    ap.add_argument("--out", required=True, help="输出 .c 路径")
    ap.add_argument("--sym", default="g_otaImage", help="数组符号名")
    ap.add_argument("--name", default="OTA_IMAGE", help="宏名（长度等）")
    args = ap.parse_args()

    if not os.path.isfile(args.bin):
        print("[ERR] 找不到 bin: %s" % args.bin)
        print("      先跑 ota/scripts/make_factory_image.py 生成 *_factory.bin")
        return 1

    with open(args.bin, "rb") as f:
        data = f.read()

    if len(data) < 32:
        print("[ERR] bin 太小（%d 字节），不像 factory 镜像（应含 32B 头）" % len(data))
        return 1

    # 简易校验：镜像头 magic（与 ota_common.h 的 OTA_IMG_MAGIC 对齐）
    magic = int.from_bytes(data[0:4], "little")
    print("[OK ] bin=%s  size=%d bytes (0x%X)" % (args.bin, len(data), len(data)))
    print("      镜像头 magic = 0x%08X" % magic)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("/* 本文件由 ota/scripts/gen_ota_image_c.py 自动生成，请勿手改。 */\n")
        f.write("/* 源镜像: %s  (%d 字节) */\n" % (os.path.basename(args.bin), len(data)))
        f.write("\n#include <stdint.h>\n\n")
        f.write("/* 目标镜像（含 32B 头），放在 .rodata 不占 RAM */\n")
        f.write("const uint8_t %s[] = {\n" % args.sym)
        emit_bytes(data, f)
        f.write("};\n\n")
        f.write("const uint32_t %sLen = %dU;\n" % (args.sym, len(data)))

    print("[OK ] 已生成: %s" % args.out)
    print("      数组 %s[%d] / %sLen" % (args.sym, len(data), args.sym))
    return 0


if __name__ == "__main__":
    sys.exit(main())