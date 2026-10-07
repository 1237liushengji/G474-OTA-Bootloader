#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_selftest.py —— 反例自证（meta-test）：证明 verify_all.py 的守卫真的挡得住回归

只证明"脚本全绿"是不够的 —— 一个永远返回 OK 的脚本也是全绿。所以这里故意注入
若干**真实可能发生的回归**，断言 verify_all.py 能以非零退出码报出对应检查项 FAIL。

安全约束：所有注入只发生在 `%TEMP%` 下的**仓库副本**里；
仓库本体（尤其 ota/app/**、ota/common/**、ota/docs/**、ota/tools/**）**绝不被写入**。
每个反例用一份全新的副本，注入 -> 运行 -> 断言 -> 删除。

用法：
    python ota/verify/verify_selftest.py           # 跑全部反例 + 基线与 --root 通路
退出码：所有反例都被抓到 = 0；有漏网/注入失败 = 1。
"""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))          # <root>/ota/verify
ROOT = os.path.dirname(os.path.dirname(HERE))              # <root>
VERIFY = os.path.join(HERE, "verify_all.py")


def force_utf8_console():
    """把 stdout/stderr 固定成 UTF-8 + errors="replace"。

    本机默认控制台是 GBK(cp936)：子进程输出里一旦出现 U+FFFD（解码时 errors="replace"
    的产物），print 会抛 UnicodeEncodeError 直接把整个自证跑挂（曾在反例 1/8 处崩溃）。
    显式切 UTF-8 既能防崩溃，也让中文在 UTF-8 终端里正常。Python < 3.7 用 TextIOWrapper 兜底。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                buf = getattr(stream, "buffer", None)
                if buf is not None:
                    setattr(sys, name, io.TextIOWrapper(buf, encoding="utf-8", errors="replace"))
        except Exception:                       # noqa: BLE001
            pass


def safe(line):
    """打印前清洗：替换字符一律降级为 '?'，保证任何控制台编码下 print 都不抛异常。"""
    if not isinstance(line, str):
        return line
    return line.replace("\ufffd", "?").encode("utf-8", "replace").decode("utf-8", "replace")


def patch_sub(rel, pattern, repl):
    """用 re.sub(count=1) 注入；返回 (kind, rel, pattern, repl)。"""
    return ("sub", rel, pattern, repl)


def patch_append(rel, text):
    """在文件末尾追加（前面留足空行，避免被 C6 的 ±6 行白名单窗口误放行）。"""
    return ("append", rel, text)


CASES = [
    ("[C1]", "契约把单块净荷退回 125",
     patch_sub("ota/common/ota_common.h",
               r"#define\s+UDS_DL_MAX_BLOCK_PAYLOAD\s+\(\(OTA_ISOTP_MAX_MSG\s*-\s*3U\)\s*&\s*~7U\)",
               "#define UDS_DL_MAX_BLOCK_PAYLOAD  125U")),
    ("[C1]", "【血线5】契约把块大小退回非 8 倍数（1021）——续传会被大面积拒绝",
     patch_sub("ota/common/ota_common.h",
               r"#define\s+UDS_DL_MAX_BLOCK_PAYLOAD\s+\(\(OTA_ISOTP_MAX_MSG\s*-\s*3U\)\s*&\s*~7U\)",
               "#define UDS_DL_MAX_BLOCK_PAYLOAD  (OTA_ISOTP_MAX_MSG - 3U)   /* = 1021，mod 8 = 5 */")),
    ("[C1]", "【血线5】删掉「块大小必须 8 字节倍数」的 #error 守卫",
     patch_sub("ota/common/ota_common.h",
               r'#if\s+\(\(UDS_DL_MAX_BLOCK_PAYLOAD\s*%\s*8U\)\s*!=\s*0U\)\s*\n'
               r'#error\s+"UDS_DL_MAX_BLOCK_PAYLOAD[^"]*"\s*\n#endif\n',
               "")),
    ("[C1]", "【血线6】删掉 UDS_DL_REQ_LEN 定义（0x34 长度校验的真相源）",
     patch_sub("ota/common/ota_common.h",
               r"#define\s+UDS_DL_REQ_LEN\s+11U",
               "/* removed */")),
    ("[C2]", "BL 上限改回字面量 128U",
     patch_sub("ota/bootloader/Core/Inc/bl_main.h",
               r"#define\s+BL_ISOTP_MAX_MSG\s+OTA_ISOTP_MAX_MSG",
               "#define BL_ISOTP_MAX_MSG          128U")),
    ("[C3]", "APP 重新引入本地上限宏 OTA_MSG_MAX",
     patch_sub("ota/app/ota_agent.c",
               r"#include\s+<string\.h>",
               "#include <string.h>\n#define OTA_MSG_MAX 128U")),
    ("[C4]", "0x34 应答写死字面量（不再引用契约宏）",
     patch_sub("ota/app/ota_agent.c",
               r"resp\[2\]\s*=\s*\(uint8_t\)\(UDS_MAXLEN_FIELD\s*>>\s*8\);\s*\n\s*"
               r"resp\[3\]\s*=\s*\(uint8_t\)\(UDS_MAXLEN_FIELD\s*&\s*0xFFU\);",
               "resp[2] = 0x00U;\n    resp[3] = 0x7DU;")),
    ("[C5]", "删掉 Ota_ReadDid 的 respMax 越界检查",
     patch_sub("ota/app/ota_agent.c",
               r"if\s*\(respMax\s*<\s*11U\)",
               "if (0U)")),
    ("[C5]", "页缓冲缩到 125 字节（不再等于 Flash 页）",
     patch_sub("ota/app/ota_agent.h",
               r"#define\s+OTA_PAGE_BUF_SIZE\s+OTA_FLASH_PAGE_SIZE",
               "#define OTA_PAGE_BUF_SIZE      125U")),
    ("[C6]", "文档残留旧吞吐数字（125 / 1835 块 / 76 秒）",
     patch_append("ota/README.md",
                  "\n\n\n\n\n\n\n\n## 回归反例（临时注入）\n"
                  "单块净荷 125 字节；满槽 1835 块；总耗时 76 秒。\n")),
    ("[C8]", "上位机把应用报文上限改回 128",
     patch_sub("ota/tools/ota_host.py",
               r"(?m)^ISOTP_MAX_MSG\s*=\s*1024",
               "ISOTP_MAX_MSG = 128")),
    # ---- C10：UDS 请求长度（2026-10-07 审查发现的真机阻断缺陷）----
    #  B1 本体：BL 侧 0x34 长度校验写成 10（实际报文 11 字节，代码自己都在读 req[10]）。
    #  真机恒回 NRC 0x13，一块固件都刷不进去；而 APP 与上位机都是 11、模拟器也是 11，
    #  导致原有 29 项守卫与全部单测都测不到。
    ("[C10]", "【B1 本体】BL 的 0x34 长度校验退回 10（应为 11）——真机 0x34 恒回 NRC 0x13",
     patch_sub("ota/bootloader/Core/Src/bl_diag.c",
               r"if\s*\(\s*len\s*!=\s*UDS_DL_REQ_LEN\s*\)",
               "if (len != 10U)")),
    ("[C10]", "【B1 本体】APP 的 0x34 长度校验退回裸字面量 11（脱离契约真相源）",
     patch_sub("ota/app/ota_agent.c",
               r"if\s*\(\s*len\s*!=\s*UDS_DL_REQ_LEN\s*\)",
               "if (len != 11U)")),
    #  B2 本体：FF00 硬性要求 6 字节，而上位机发 4 字节 -> 整槽刷写在第一步就被 NRC 0x13 中断
    ("[C10]", "【B2 本体】BL 的 FF00 长度校验退回仅接受 6U（上位机发 4 字节 -> 流程中断）",
     patch_sub("ota/bootloader/Core/Src/bl_diag.c",
               r"if\s*\(\(len\s*!=\s*UDS_RID_ERASE_REQ_LEN_MIN\)\s*&&\s*"
               r"\(len\s*!=\s*UDS_RID_ERASE_REQ_LEN_MAX\)\)",
               "if (len != 6U)")),
    #  B2 回归：上位机重新单独发 FF00（与 0x34 重复擦除，吃掉超时预算）
    ("[C10]", "【B2 回归】上位机重新单独发 FF00（与 0x34 隐式擦除重复，吃掉超时预算）",
     patch_sub("ota/tools/ota_host.py",
               r"(?m)^(\s*)self\.log\(\"擦除由 0x34 隐式完成（不再单独发 FF00）\"\)",
               "\\1self.uds.routine_control(UDS_ROUTINE_ERASE)\n"
               "\\1self.log(\"擦除应用区 31 01 FF00\")")),
    # ---- C9：BL 链接布局（2026-10-05 变砖事故的两个开关，各留一个反例）----
    ("[C9]", "bl.sct 重现事故布局：ER_IROM1 从 0x08000000 起 0xB800（代码铺进控制区页）",
     patch_sub("ota/bootloader/MDK-ARM/bl.sct",
               r"ER_IROM1 0x08000000 0x000001D8",
               "ER_IROM1 0x08000000 0x0000B800")),
    ("[C9]", "BL.uvprojx 把 umfTarg 改回 1（µVision 无视 bl.sct 自行生成）",
     patch_sub("ota/bootloader/MDK-ARM/BL.uvprojx",
               r"<umfTarg>0</umfTarg>",
               "<umfTarg>1</umfTarg>")),
    # ---- C11：BL->APP 交接与流控节拍（2026-10-07 第二梯队）----
    #  H1 本体：跳转前只清 NVIC->ICPR，不清 SCB->ICSR 的系统异常挂起位。
    #  SysTick 1ms 周期，改 VTOR 后残留 pending 会让 APP 在初始化完成前按自己的
    #  向量表取向量 -> 随机 HardFault（约千分之一概率，极难复现）。
    ("[C11]", "【H1 本体】bl_jump.c 删掉 SCB->ICSR 清理（系统异常挂起位 NVIC 清不到）",
     patch_sub("ota/bootloader/Core/Src/bl_jump.c",
               r"SCB\s*->\s*ICSR\s*=\s*SCB_ICSR_PENDSTCLR_Msk\s*\|\s*SCB_ICSR_PENDSVCLR_Msk\s*;",
               "/* removed: 误以为 NVIC->ICPR 已经清干净了 */")),
    ("[C11]", "【H1 本体】只清 PENDSTCLR，漏掉 PENDSVCLR",
     patch_sub("ota/bootloader/Core/Src/bl_jump.c",
               r"SCB\s*->\s*ICSR\s*=\s*SCB_ICSR_PENDSTCLR_Msk\s*\|\s*SCB_ICSR_PENDSVCLR_Msk\s*;",
               "SCB->ICSR = SCB_ICSR_PENDSTCLR_Msk;")),
    #  H3 本体：全部副本无效的回退路径裸写 PRIMARY 页，无视 BL_PARAM_PRIMARY_WRITE。
    #  典型触发场景=全新芯片首次上电（Flash 全 0xFF），正是该宏最需要生效的时刻。
    ("[C11]", "【H3 本体】全部副本无效的回退路径删掉 #if 保护（裸写 PRIMARY 页）",
     patch_sub("ota/bootloader/Core/Src/bl_param.c",
               r"#if\s*\(BL_PARAM_PRIMARY_WRITE\s*==\s*0\)\s*\n\s*"
               r"\*page\s*=\s*OTA_CFG_MIRROR_ADDR;\s*\n#else\s*\n\s*"
               r"\*page\s*=\s*OTA_CFG_PRIMARY_ADDR;\s*\n#endif",
               "*page = OTA_CFG_PRIMARY_ADDR;")),
    #  H2 本体：同页分支恒写副本0 -> 副本1 永不写，双副本退化为单副本，血线1 形同虚设
    ("[C11]", "【H2 本体】同页分支恒写副本0（副本1 永不写，双副本退化为单副本）",
     patch_sub("ota/bootloader/Core/Src/bl_param.c",
               r"\*off\s*=\s*\(s_candOff\[newestIdx\]\s*==\s*OTA_CFG_SLOT_OFFSET\)\s*\n\s*"
               r"\?\s*OTA_CFG_SLOT_MIRROR_OFF\s*\n\s*:\s*OTA_CFG_SLOT_OFFSET\s*;",
               "*off  = OTA_CFG_SLOT_OFFSET;")),
    #  H4 本体：只读 FC 的 BS，忽略 STmin -> 146 帧背靠背，BL 的 RX FIFO0仅 3 深度
    ("[C11]", "【H4 本体】上位机忽略 FC 的 STmin（146 帧背靠背，BL RX FIFO 仅 3 深度）",
     patch_sub("ota/tools/ota_host.py",
               r"stmin_s\s*=\s*isotp_stmin_to_seconds\(raw_stmin\)",
               "stmin_s = 0.0  # 忽略 STmin")),
]


def make_copy():
    """把 <root>/ota 复制到临时目录，返回临时根（其下有 ota/）。"""
    tmp = tempfile.mkdtemp(prefix="ota_verify_neg_")
    shutil.copytree(os.path.join(ROOT, "ota"), os.path.join(tmp, "ota"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp


def apply_patch(tmp, patch):
    kind = patch[0]
    rel = patch[1]
    path = os.path.join(tmp, rel)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if kind == "sub":
        pattern, repl = patch[2], patch[3]
        new, n = re.subn(pattern, repl, text, count=1)
        if n != 1:
            return False, "注入点未匹配（模式 %r）" % pattern
    else:
        new = text + patch[2]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
    return True, ""


def run_verify(root):
    """跑一次 verify_all.py --root <副本>。

    env 里显式给 PYTHONIOENCODING=utf-8 / PYTHONUTF8=1：让子进程按 UTF-8 输出，
    本脚本再按 UTF-8 解码，从源头上避免"子进程按 GBK 输出、本脚本按 UTF-8 解码"
    产生满屏 U+FFFD（那正是上一版崩溃的根因之一）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, VERIFY, "--root", root],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", env=env)
    return proc.returncode, proc.stdout or ""


def main():
    force_utf8_console()
    print("=" * 78)
    print("反例自证 verify_selftest.py —— 证明 verify_all.py 能抓住回归")
    print("仓库根: %s" % ROOT)
    print("每个反例都在 %%TEMP%% 下的独立副本上注入，仓库本体不被写入")
    print("=" * 78)

    ok_all = True

    # ---------------- 基线：未注入的副本必须全绿（同时验证 --root 通路可用）
    tmp = make_copy()
    try:
        rc, out = run_verify(tmp)
        tail = [ln for ln in out.splitlines() if ln.startswith("汇总")]
        base_ok = (rc == 0)
        ok_all = ok_all and base_ok
        print("[%s] 基线（未注入）: 退出码 %d | %s"
              % ("OK " if base_ok else "FAIL", rc, safe(tail[-1]) if tail else "<无汇总>"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 逐个反例
    caught = 0
    for idx, (cid, name, patch) in enumerate(CASES, 1):
        tmp = make_copy()
        try:
            applied, why = apply_patch(tmp, patch)
            if not applied:
                ok_all = False
                print("[FAIL] 反例 %d/%d %s %s -> 注入失败: %s" % (idx, len(CASES), cid, name, why))
                continue
            rc, out = run_verify(tmp)
            hit = ("[FAIL] %s" % cid) in out
            good = hit and (rc != 0)
            caught += 1 if good else 0
            ok_all = ok_all and good
            fails = [ln.strip() for ln in out.splitlines() if ln.startswith("[FAIL]")]
            print("[%s] 反例 %d/%d %s %-38s -> 退出码 %d, 命中 %s"
                  % ("OK " if good else "FAIL", idx, len(CASES), cid, name, rc,
                     "是" if hit else "否"))
            for ln in fails[:3]:
                print(safe("         被抓到: " + ln))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 78)
    print("汇总: %d 个反例, %d 个被抓到, %d 个漏网 -> %s"
          % (len(CASES), caught, len(CASES) - caught, "自证通过" if ok_all else "自证失败"))
    print("=" * 78)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
