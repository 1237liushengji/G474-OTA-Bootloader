#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【2026-10-12】一键 A/B 双角色构建 + 镜像生成 + 烧录。

背景：G474.uvprojx 用 <Define> 里的 CAN_NODE_ROLE 切换主/从，两块板必须分别编译；
      板 A 还要把"板 B 的 factory 镜像"编进 .rodata（gen_ota_image_c.py），
      所以存在 B 先编 → 生成数组 → A 再编 的顺序依赖。

用法：
  python ota/scripts/build_ab.py            # 全流程（编 B → 嵌 → 编 A → 双烧录）
  python ota/scripts/build_ab.py --no-flash # 只构建不烧录
  python ota/scripts/build_ab.py --role-b   # 只重编板 B
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UV4 = "D:/U5/UV4/UV4.exe"
PROJ = os.path.join(ROOT, "G474", "MDK-ARM", "G474.uvprojx")
FW = os.path.join(ROOT, "firmware")
PROG = "D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"

SN_A = "8"
SN_B = "49FF6D068065575312232167"

DEF_A = ("USE_HAL_DRIVER,STM32G474xx,USER_VECT_TAB_ADDRESS,VECT_TAB_OFFSET=0xC080,"
         "CAN_NODE_ROLE=CAN_NODE_A,APP_BOARD_FP=FP_A1,OTA_CLIENT_ENABLE=1")
DEF_B = ("USE_HAL_DRIVER,STM32G474xx,USER_VECT_TAB_ADDRESS,VECT_TAB_OFFSET=0xC080,"
         "CAN_NODE_ROLE=CAN_NODE_B,APP_BOARD_FP=FP_B2,OTA_CLIENT_ENABLE=0")


def set_define(text):
    return re.sub(r"<Define>[^<]*</Define>",
                  "<Define>%s</Define>" % text, text, count=1)


def build(role, version, flash, sn):
    """role: 'A'|'B'"""
    define = DEF_A if role == "A" else DEF_B
    src = open(PROJ, encoding="utf-8").read()
    # 只替换第一处 <Define>（第二个是空的，Keil 结构如此）
    out = re.sub(r"<Define>[^<]*</Define>",
                 lambda m: "<Define>%s</Define>" % define,
                 src, count=1)
    open(PROJ, "w", encoding="utf-8").write(out)

    wd = os.path.join(ROOT, "G474", "MDK-ARM")
    subprocess.run([UV4, "-r", "G474.uvprojx", "-j0", "-o", "build.log"],
                   cwd=wd, capture_output=True)
    log = open(os.path.join(wd, "build.log"), encoding="utf-8", errors="ignore").read()
    m = re.search(r"(\d+) Error\(s\), (\d+) Warning\(s\)", log)
    if not m or m.group(1) != "0":
        print(log[-3000:])
        raise SystemExit("[%s] 编译失败" % role)
    print("[%s] 编译 OK  %s" % (role, re.search(r"Program Size:.*", log).group(0)))

    # 保留该角色的 map（两构建符号地址不同，必须各自留存）
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    shutil.copy(os.path.join(wd, "G474", "G474.map"),
                os.path.join(ROOT, "logs", "map_node%s.map" % role))

    subprocess.run([sys.executable, os.path.join(ROOT, "ota", "scripts", "make_factory_image.py"),
                    "--hex", os.path.join(wd, "G474", "G474.hex"),
                    "--slot", "A", "--version", version],
                   cwd=ROOT, capture_output=True)
    gen_dir = os.path.join(wd, "G474")
    hexf = os.path.join(gen_dir, "G474_factory.hex")
    binf = os.path.join(gen_dir, "G474_factory.bin")
    for src_, dst_ in ((hexf, "G474_%s_factory.hex" % role),
                       (binf, "G474_%s_factory.bin" % role)):
        shutil.copy(src_, os.path.join(FW, dst_))

    if flash:
        r = subprocess.run([PROG, "-c", "port=SWD", "sn=" + sn, "mode=UR",
                            "-w", hexf, "-v", "-rst"], capture_output=True, text=True, errors="replace")
        out = r.stdout or ""
        ok = "verified successfully" in out
        print("[%s] 烧录 %s  sn=%s" % (role, "OK" if ok else "FAIL", sn))
        if not ok:
            print(out[-2000:], (r.stderr or "")[-1000:])
            raise SystemExit("[%s] 烧录失败" % role)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-flash", action="store_true")
    ap.add_argument("--role-b", action="store_true", help="只编板 B")
    ap.add_argument("--version", default="3.6")
    a = ap.parse_args()

    if not a.role_b:
        # 1) 板 B 优先（它的镜像要被编进板 A）
        build("B", a.version, not a.no_flash, SN_B)
        # 2) 把 B 的 bin 转成 const 数组
        subprocess.run([sys.executable, os.path.join(ROOT, "ota", "scripts", "gen_ota_image_c.py"),
                        "--bin", os.path.join(FW, "G474_B_factory.bin"),
                        "--out", os.path.join(ROOT, "G474", "Drivers", "User", "Src", "ota_image_data.c"),
                        "--sym", "g_otaImage"], cwd=ROOT)
        # 3) 板 A（内嵌上面这份镜像）
        build("A", a.version, not a.no_flash, SN_A)
    else:
        build("B", a.version, not a.no_flash, SN_B)


if __name__ == "__main__":
    main()